from __future__ import annotations

import atexit
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import string
import threading
import time
import uuid
from collections import deque
from datetime import datetime
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
from .accounts.crypto import (
    MasterKeyError,
    PURPOSE_APP_SESSION,
    derive_key,
    master_key_from_env,
)
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
)
from .database import Database
from .time_utils import utc_now_iso, utc_now_iso_offset
from .importers.cloud139 import Cloud139Importer
from .importers.generic import GenericWebhookImporter
from .importers.quark import QuarkImporter
from .importers.sixpan import SixPanOfflineImporter
from .media.fnos import FnosMediaRefresher
from .logging_setup import install_file_log_handler
from .blueprints import AdaptersRouteContext, AdminShellRouteContext, AuthRouteContext, CallbackRouteContext, DiagnosticsRouteContext, JobsRouteContext, MediaRouteContext, OrganizerRouteContext, PublicRouteContext, RcloneRouteContext, RcloneWorkerControlContext, RequestsRouteContext, SettingsRouteContext, SixPanRouteContext, SystemRouteContext, UpdatesRouteContext, create_adapters_blueprint, create_admin_shell_blueprint, create_auth_blueprint, create_callbacks_blueprint, create_diagnostics_blueprint, create_jobs_blueprint, create_media_blueprint, create_organizer_blueprint, create_public_blueprint, create_rclone_blueprint, create_rclone_worker_control_blueprint, create_requests_blueprint, create_settings_blueprint, create_sixpan_blueprint, create_system_blueprint, create_updates_blueprint
from .organizer.openlist_client import (
    VIDEO_EXTENSIONS,
    OpenListClient,
    api_base_url as openlist_api_base_url,
)
from .organizer.service import OrganizerService, build_frozen_openlist_client_resolver
from .providers.btbtla import BtbtlaClient
from .providers.hot_sources import IqiyiHotSource, TencentHotSource, YoukuHotSource
from .providers.active import ActiveProviderUnavailable, get_optional_provider_method
from .services.import_service import ImportService
from .services.job_service import JobService
from .services.rclone_service import RcloneService
from .services.rclone_webdav_config_service import RcloneWebdavConfigError, RcloneWebdavConfigService
from .services.rclone_worker_client import RcloneWorkerClient, RcloneWorkerRequestError
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
from .repositories import (
    JobRepositories,
    NotificationRepositories,
    OrganizerRepositories,
    RcloneRepositories,
    RepositorySet,
    UpdateRepositories,
)
from .services.search_service import SearchService
from .services.update_scheduler import UpdateScheduler
from .services.update_scheduler_runtime import UpdateSchedulerRuntime
from .services.trending_discovery_service import TrendingDiscoveryService
from .services.trending_discovery_scheduler import TrendingDiscoveryScheduler
from .services.trending_initial_import_service import TrendingInitialImportError, TrendingInitialImportService
from .services import trending_subscription_helpers as hot_subscription
from .services.import_completion_dispatcher import ImportCompletionDispatcher
from .services.execution_plan_guard import require_v1_execution_plan
from .services.job_completion_info_service import JobCompletionInfoService
from .services.runtime_reload_service import RuntimeReloadService, finalize_organizer_runtime_transition
from .services.public_submission_preparation_service import (
    PublicSubmissionPreparationError,
    PublicSubmissionPreparationService,
)
from .services.public_import_job_coordinator import PublicImportJobCoordinator
from .services.public_submission_decision_service import PublicSubmissionDecisionService
from .services.public_submission_intake_service import PublicSubmissionIntakeService
from .services.public_tmdb_service import PublicTmdbService
from .services.security_status_service import SecurityStatusService
from .services.rate_limit_service import RateLimitService
from .services.public_resource_detail_service import PublicResourceDetailService
from .services.media_dashboard_service import (
    MediaDashboardService,
)
from .services.durable_worker_runtime import DurableWorkerRuntime
from .services.worker_task_dispatcher import WorkerTaskDispatcher
from .services.worker_queue_diagnostics_service import WorkerQueueDiagnosticsService
from .services.readiness_service import ReadinessService, routes_require_openlist
from .services.worker_watchdog import WorkerWatchdog
from .services.notification_settings_service import NotificationSettingsService
from .notifications import events as notification_events
from .notifications import config as notification_config
from .notifications import secrets as notification_secrets
from .notifications.emitter import emit_notification
from .notifications.scheduler import NotificationDigestScheduler
from .notifications.transitions import emit_organizer_review_required
from .notifications.worker import make_notification_deliver_handler
from .services.public_submission_preflight_service import PublicSubmissionPreflightService
from .search.aggregator import SearchProvidersUnavailableError
from .services.public_manual_preview_service import PublicManualPreviewService
from .services.public_bt_resolve_service import PublicBtResolveService
from .services.public_sixpan_preview_service import PublicSixpanPreviewService
from .services.update_service import UpdateService
from .runtime import RuntimeServices, RuntimeSnapshot, install_request_runtime
from .process_role import resolve_process_role, role_runs
from .runtime_builder import (
    RuntimeBuilder,
    RuntimeInstaller,
    RuntimeRetirementQueue,
    rclone_runtime_config,
)
from .blueprints.trending import TrendingRouteContext, create_trending_blueprint
from .transfer.recovery import TransferRecoveryRuntime, TransferRecoveryScanner

from .media_serializers import (
    _build_media_dashboard,
)
from .path_planning import (
    _clean_update_openlist_root,
    _resource_update_root,
)
from .public_web import (
    _adapter_placeholders,
    _auto_submit_allowed,
    _detail_capability_for_source,
    _elapsed_ms,
    _guest_safe_job_result,
    _hash_client_ip,
    _hash_password,
    _new_public_id,
    _new_request_token,
    _new_simple_captcha,
    _normalize_submission_mode,
    _preflight_public_submission,
    _public_adapter_capabilities,
    _public_cached_item,
    _public_categories,
    _public_request_payload,
    _public_request_response,
    _public_resource_child_files,
    _public_resource_detail,
    _public_resource_item,
    _public_result_provider_badges,
    _public_security_config,
    _public_status,
    _public_submit_message,
    _rclone_callback_level,
    _safe_public_cloud139_selection,
    _safe_public_quark_selection,
    _safe_public_sixpan_selection,
    _safe_public_tianyi_selection,
    _safe_public_string_list,
    _short_text,
    _sixpan_parse_summary,
    _verify_password_hash,
    _verify_public_captcha,
)
from .web_input import (
    PublicInputError,
    _clip_text,
    _config_bool,
    _config_int,
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
    _validate_public_url,
    _worker_dispatch_enabled_for_role,
)

__all__ = ["create_app"]


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
def create_app(config_path: str | None = None, process_role: str | None = None) -> Flask:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    app_config = load_config(config_path)
    try:
        root_key = master_key_from_env()
    except MasterKeyError as exc:
        raise RuntimeError(
            "FNOS_ROOT_KEY 未配置或无效；无法启动应用"
        ) from exc
    # Flask sessions use a purpose-isolated key derived from the one required
    # application root key.  It is intentionally never persisted or accepted
    # through YAML/environment compatibility fields.
    app_session_key = derive_key(root_key, PURPOSE_APP_SESSION).hex()
    active_process_role = resolve_process_role(process_role)
    process_owner_id = f"{os.getenv('HOSTNAME') or os.getenv('COMPUTERNAME') or 'host'}:{os.getpid()}:{secrets.token_hex(6)}"
    database = Database(app_config.database_path)
    database.init_schema()
    db = RepositorySet.from_database(database)
    organizer_repositories = OrganizerRepositories.from_set(db)
    rclone_repositories = RcloneRepositories.from_set(db)
    job_repositories = JobRepositories.from_set(db)
    update_repositories = UpdateRepositories.from_set(db)
    notification_repositories = NotificationRepositories.from_set(db)
    admin_config = app_config.raw.get("admin", {}) if isinstance(app_config.raw.get("admin"), dict) else {}
    admin_profile_service = AdminProfileService(
        db.admin_profiles,
        admin_config,
        app_config.base_dir / "static" / "uploads",
        _hash_password,
        _verify_password_hash,
    )
    persisted_settings = db.app_settings.get_all()
    app_config = apply_persisted_config(app_config, persisted_settings)

    # Build the complete V1 dependency graph before RuntimeBuilder.  Both the
    # initial build and hot-reload candidates are immediately able to create,
    # retry and execute immutable-plan jobs; no field-level rewire follows.
    from .accounts.service import ProviderAccountService
    from .openlist.connection_service import OpenListConnectionService
    from .openlist.mount_service import OpenListMountService
    from .providers.adapters import (
        MobileProviderAdapter,
        QuarkProviderAdapter,
        SixPanProviderAdapter,
        TianyiProviderAdapter,
    )
    from .providers.registry import ProviderRegistry
    from .services.source_record_service import SourceRecordService
    from .storage.assignment_service import SourceAssignmentService
    from .storage.target_service import StorageTargetService
    from .transfer.execution_repository import TransferExecutionRepository

    def _v1_master_key() -> bytes:
        return root_key

    v1_accounts_service = ProviderAccountService(db, _v1_master_key)
    v1_openlist_connections = OpenListConnectionService(db, _v1_master_key)
    v1_openlist_mounts = OpenListMountService(
        db,
        connection_service=v1_openlist_connections,
    )
    v1_target_service = StorageTargetService(
        db,
        openlist_dir_lister=lambda mount_id, path: v1_openlist_mounts.list_mountable_dirs(
            mount_id, path
        )
        if mount_id
        else [],
    )
    v1_rules_service = SourceAssignmentService(db, v1_target_service)
    v1_source_record_service = SourceRecordService(db, _v1_master_key)
    v1_execution_repository = TransferExecutionRepository(db)

    def _account_context(
        account_id: str,
        revision: int,
        expected_provider: str = "",
    ) -> dict[str, Any]:
        account_id = str(account_id or "").strip()
        revision = int(revision or 0)
        if not account_id or revision <= 0:
            raise RuntimeError("Provider 账号修订身份不完整")
        context = v1_accounts_service.resolve_account_context(account_id, revision)
        provider_kind = str(context.get("provider_kind") or "").strip()
        if expected_provider and provider_kind != expected_provider:
            raise RuntimeError("Provider 账号类型与执行计划不一致")
        return context

    def _runtime_credential_persister(
        account_id: str,
        revision: int,
    ) -> Callable[[dict[str, Any]], bool]:
        revision_state = {"value": int(revision)}
        revision_lock = threading.Lock()

        def persist(patch: dict[str, Any]) -> bool:
            if not isinstance(patch, dict) or not patch:
                return False
            try:
                with revision_lock:
                    updated = v1_accounts_service.refresh_runtime_credentials(
                        account_id,
                        patch,
                        expected_revision=revision_state["value"],
                    )
                    revision_state["value"] = int(updated.current_revision)
                return True
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "Provider 运行时凭证持久化失败 account=%s revision=%s: %s",
                    account_id,
                    revision_state["value"],
                    exc,
                )
                return False

        return persist

    def _provider_importer(
        provider_kind: str,
        account_id: str,
        revision: int,
    ) -> Any:
        context = _account_context(account_id, revision, provider_kind)
        config_values = dict(context.get("config") or {})
        credentials = dict(context.get("credentials") or {})
        values = {**config_values, **credentials}
        persist_credentials = _runtime_credential_persister(account_id, revision)
        if provider_kind == "quark":
            return QuarkImporter(values, cookie_update_callback=persist_credentials)
        if provider_kind == "tianyi":
            from .importers.tianyi import TianyiImporter

            return TianyiImporter(
                values,
                token_update_callback=persist_credentials,
                cache_namespace=f"account:{account_id}",
            )
        if provider_kind == "mobile":
            return Cloud139Importer(config_values, cmcc_config=values)
        if provider_kind == "sixpan":
            return SixPanOfflineImporter(
                values,
                token_update_callback=persist_credentials,
            )
        raise RuntimeError(f"不支持的 Provider：{provider_kind}")

    def _active_provider_importer(provider_kind: str) -> Any:
        selection = v1_accounts_service.get_selection(provider_kind)
        if selection is None:
            raise RuntimeError(f"当前 {provider_kind} 账号未配置或未激活")
        return _provider_importer(
            provider_kind,
            selection.account_id,
            selection.account_revision,
        )

    def _quark_importer_for_account(account_id: str, revision: int) -> Any:
        return _provider_importer("quark", account_id, revision)

    def _tianyi_importer_for_account(account_id: str, revision: int) -> Any:
        return _provider_importer("tianyi", account_id, revision)

    def _mobile_importer_for_account(account_id: str, revision: int) -> Any:
        return _provider_importer("mobile", account_id, revision)

    def _sixpan_importer_for_account(account_id: str, revision: int) -> Any:
        return _provider_importer("sixpan", account_id, revision)

    from .importers.tianyi import TianyiImporter

    v1_adapter_registry = ProviderRegistry(
        [
            QuarkProviderAdapter(
                importer_factory=_quark_importer_for_account,
                account_importer_factory=QuarkImporter,
                source_record_service=v1_source_record_service,
            ),
            TianyiProviderAdapter(
                importer_factory=_tianyi_importer_for_account,
                account_importer_factory=TianyiImporter,
                source_record_service=v1_source_record_service,
            ),
            MobileProviderAdapter(
                importer_factory=_mobile_importer_for_account,
                account_importer_factory=lambda values: Cloud139Importer(
                    values,
                    cmcc_config=values,
                ),
                source_record_service=v1_source_record_service,
            ),
            SixPanProviderAdapter(
                importer_factory=_sixpan_importer_for_account,
                account_importer_factory=SixPanOfflineImporter,
                source_record_service=v1_source_record_service,
            ),
        ]
    )
    v1_adapter_registry.load_entry_points(
        {
            "database": db,
            "accounts_service": v1_accounts_service,
            "source_record_service": v1_source_record_service,
            "master_key_provider": _v1_master_key,
        }
    )
    v1_accounts_service.set_catalog_provider(v1_adapter_registry.manifests)
    v1_accounts_service.set_tester(v1_adapter_registry.account_tester)
    v1_rules_service.set_source_catalog_provider(v1_adapter_registry.search_sources)

    def _resolve_organizer_openlist_client(
        job: dict[str, Any],
        plan: dict[str, Any],
    ) -> OpenListClient:
        runtime_openlist = (
            app_config.raw.get("openlist", {})
            if isinstance(app_config.raw.get("openlist"), dict)
            else {}
        )
        return build_frozen_openlist_client_resolver(
            v1_openlist_mounts,
            v1_openlist_connections,
            runtime_openlist,
        )(job, plan)

    # The dispatcher is safe to construct before RuntimeServices: its revision
    # callback is evaluated only when a request enqueues work.
    worker_task_dispatcher = WorkerTaskDispatcher(
        repository=db.worker_tasks,
        enabled=lambda: _worker_dispatch_enabled_for_role(
            app_config.raw.get("worker", {})
            if isinstance(app_config.raw.get("worker"), dict)
            else {},
            active_process_role,
        ),
        config_revision=lambda: runtime_services.revision,
    )

    runtime_builder = RuntimeBuilder(
        db,
        organizer_repositories=organizer_repositories,
        owner_id=process_owner_id,
        organizer_openlist_client_resolver=_resolve_organizer_openlist_client,
        provider_importer_resolver=_active_provider_importer,
        accounts_service=v1_accounts_service,
        target_service=v1_target_service,
        mount_service=v1_openlist_mounts,
        rules_service=v1_rules_service,
        provider_registry=v1_adapter_registry,
        source_record_service=v1_source_record_service,
        execution_repository=v1_execution_repository,
        enqueue_transfer=lambda job_id: worker_task_dispatcher.import_transfer_execute(
            job_id,
            reactivate=True,
        ),
        enqueue_transfer_with_connection=(
            worker_task_dispatcher.import_transfer_execute_with_connection
        ),
        config_revision_provider=lambda: runtime_services.revision,
    )
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
    )

    job_service = JobService(job_repositories)
    rclone_service = RcloneService(
        rclone_runtime_config(app_config),
        app_config.base_dir,
        app_config.raw["fnos"],
        db=rclone_repositories,
        categories=app_config.categories,
        # Credentials, mounts and targets are resolved from the V1 database
        # graph.  These constructor slots are retained only for non-secret
        # runtime tuning; never repopulate them from removed global sections.
        cmcc_upload_config={},
        cloud139_config={},
        storage_config={},
        owner_id=process_owner_id,
    )
    rclone_worker_client: RcloneWorkerClient | None = None
    if active_process_role == "web":
        rclone_worker_client = RcloneWorkerClient(
            base_url=os.getenv("RCLONE_WORKER_URL", "http://fnos-rclone-worker:5251"),
            token=os.getenv("RCLONE_WORKER_CONTROL_TOKEN", ""),
            database=rclone_repositories,
            timeout_seconds=_safe_int(os.getenv("RCLONE_WORKER_TIMEOUT_SECONDS"), 30, 3, 300),
        )
        atexit.register(rclone_worker_client.close)
    rclone_controller = rclone_worker_client or rclone_service
    rclone_webdav_config_service = RcloneWebdavConfigService(lambda: rclone_service.config)
    update_service = UpdateService(
        update_repositories,
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
        process_role=active_process_role,
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
            iqiyi_device_id = hashlib.sha256(
                f"{app_session_key}:{process_owner_id.split(':', 1)[0]}".encode("utf-8")
            ).hexdigest()[:32]
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
        key = hot_subscription.title_key(title)
        if not key:
            return False
        return any(
            hot_subscription.identity_matches(item, job)
            for job in db.job_queries.list(limit=100, status=JOB_DONE, keyword=title)
        )

    def _hot_existing_subscription(item: dict[str, Any]) -> dict[str, Any] | None:
        if not hot_subscription.title_key(item.get("title")):
            return None
        return hot_subscription.find_existing_subscription(
            item,
            db.update.list_update_subscriptions(limit=5000, include_sources=False),
        )

    def _hot_task_exists(item: dict[str, Any]) -> bool:
        if _hot_existing_subscription(item):
            return True
        key = hot_subscription.title_key(item.get("title"))
        if not key:
            return False
        return any(
            hot_subscription.identity_matches(item, job)
            for job in db.job_queries.list(limit=100, keyword=str(item.get("title") or ""))
            if str(job.get("status") or "") not in {JOB_DONE, JOB_FAILED, JOB_CANCELLED}
        )

    trending_service = TrendingDiscoveryService(
        sources=hot_sources,
        repository=db.trending,
        scheduler_leases=db.scheduler_leases,
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
    rclone_worker_runtime = RcloneWorkerRuntime(
        rclone_service=rclone_service,
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

    notification_deliver_handler = make_notification_deliver_handler(
        notification_repositories
    )
    notification_digest_scheduler = NotificationDigestScheduler(
        database=notification_repositories,
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
                notification_repositories,
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
            notification_repositories,
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
                notification_repositories,
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
        # Every completion path (native, relay and rclone) enters the same
        # durable V1 transfer queue.  The execution repository is attached
        enqueue_transfer=worker_task_dispatcher.import_transfer_execute,
        execution_repository=v1_execution_repository,
    )

    def _auto_start_rclone_for_import(result: dict[str, Any], reason: str) -> dict[str, Any] | None:
        return import_completion_dispatcher.dispatch(result, reason)

    if role_runs(active_process_role, "scheduler"):
        update_scheduler_runtime.start()
        trending_scheduler.start()
        notification_digest_scheduler.start()
















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
                empty_message="V1 execution_plan 未固化 rclone 目标路径",
            )
        _add("openlist_visible_path", "OpenList 可见路径", openlist_visible_path, required=openlist_required)
        _add("organizer_scan_path", "Organizer 扫描路径", organizer_scan_path, required=organizer_required)
        _add("organized_target_path", "标准整理目标路径", organized_target_path, required=False)
        if not organizer_service.enabled:
            checks.append({"name": "organizer_enabled", "label": "Organizer", "success": False, "message": "Organizer 未启用，不能确认完整整理入库"})
        if not bool(organizer_service.status().get("openlist_configured")):
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




    job_completion_info_service = JobCompletionInfoService(
        common_virtual_path=_common_virtual_path,
        path_health_checks=_path_health_checks,
        plan_loader=lambda job: require_v1_execution_plan(
            job,
            execution_repository=getattr(
                app.extensions.get("v1_transfer_dispatch"), "executions", None
            ),
        ),
    )


    app = Flask(
        __name__,
        template_folder=str(app_config.base_dir / "templates"),
        static_folder=str(app_config.base_dir / "static"),
    )
    # 让 JSON 响应直接输出 UTF-8 中文，而不是 \\uXXXX 转义（Flask 默认 ensure_ascii=True）。
    app.json.ensure_ascii = False
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
    install_file_log_handler(root_logger, app_config.base_dir)
    app.secret_key = app_session_key
    app.config["APP_CONFIG"] = app_config
    app.config["DATABASE"] = db
    app.config["PROCESS_ROLE"] = active_process_role
    if Path("/var/run/docker.sock").exists():
        app.logger.critical(
            "检测到 Docker Socket 挂载；当前进程继续启动但 readiness 将保持失败，请替换为无 Socket 的当前编排"
        )
    security_config = app_config.raw.get("security", {})
    public_tmdb_service = PublicTmdbService(
        client=lambda: organizer_service.tmdb,
        secret_key=str(app.secret_key or ""),
    )
    if not str(security_config.get("ip_hash_salt") or "").strip():
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
        get_resource=db.resources.get,
        get_cached_resource=db.search_cache_queries.get_active,
        find_resource_by_url=db.resources.find_by_url,
        search_resources=lambda **kwargs: _public_search_application(admin=True).search(
            kwargs["keyword"],
            sources=kwargs.get("sources") or [],
            token=kwargs.get("token") or "",
            options=kwargs.get("options") or {},
            hide_full_links=False,
            trace_id=kwargs.get("trace_id") or "-",
            cache_keyword=kwargs.get("cache_keyword"),
        ),
        get_job=db.job_queries.get,
        categories=lambda: app_config.categories,
        runtime_revision=lambda: runtime_services.revision,
        executor_id=lambda: active_process_role,
        start_import=lambda result, reason: _auto_start_rclone_for_import(result, reason),
        sanitize_string_list=lambda value: _safe_public_string_list(value, max_items=2000, max_length=512),
        sanitize_quark_selection=_safe_public_quark_selection,
        sanitize_cloud139_selection=_safe_public_cloud139_selection,
        resolve_destination=None,
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
        load_config=lambda: apply_persisted_config(load_config(config_path), db.app_settings.get_all()),
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
        install_candidate=lambda build, config: runtime_installer.install(
            build, config
        ),
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
            previous_build=previous_build,
            candidate_build=runtime_build,
            retirement=runtime_retirement,
            activate_background=role_runs(active_process_role, "worker"),
        )
        if rclone_worker_client is not None:
            worker_reload = rclone_worker_client.reload()
            outcome.response["rclone_worker_reload"] = worker_reload
            if not worker_reload.get("success"):
                status_code = _safe_int_value(worker_reload.get("_http_status"), 503)
                raise RcloneWorkerRequestError(
                    f"rclone Worker 重载失败：{worker_reload.get('message') or '未知错误'}",
                    status_code,
                    worker_reload,
                )
        return outcome.response

    app.extensions["reload_runtime_config"] = _reload_runtime_config

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
        settings=db.app_settings.get_all,
        strict_enabled=_strict_security_enabled,
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
        job = job if job is not None else db.job_queries.get(int(item["job_id"]))
        if not job:
            return item
        job_status = str(job.get("status") or "")
        public_status = _public_status(job_status)
        if not job_status or (job_status == request_status and str(item.get("public_status") or "") == public_status):
            return item
        if job_status != request_status:
            db.guest_request_commands.add_event(
                int(item["id"]),
                "info",
                "系统同步关联正式任务状态",
                {"job_id": job.get("id"), "job_status": job_status, "previous_status": request_status},
            )
        db.guest_request_commands.update(
            int(item["id"]),
            status=job_status,
            public_status=public_status,
            raw_data=_merge_raw_data(item.get("raw_data"), {"status_sync": {"job_id": job.get("id"), "job_status": job_status}}),
        )
        return db.guest_request_queries.get(int(item["id"])) or item

    def _sync_guest_request_statuses(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        job_ids = []
        for item in items:
            if not item.get("job_id") or str(item.get("status") or "") in {"rejected", "cancelled", "unsupported"}:
                continue
            job_id = _safe_int(item.get("job_id"), 0, 1, 999999999)
            if job_id:
                job_ids.append(job_id)
        jobs_by_id = db.job_queries.get_many(job_ids)
        return [
            _sync_guest_request_status(item, jobs_by_id.get(_safe_int(item.get("job_id"), 0, 1, 999999999))) if item.get("job_id") else item
            for item in items
        ]

    def _sync_guest_requests_for_job(job_id: int, job_status: str, raw: dict[str, Any] | None = None) -> None:
        if not job_id:
            return
        public_status = _public_status(job_status)
        for item in db.guest_request_queries.list_by_job(job_id):
            request_id = _safe_int(item.get("id"), 0, 0, 999999999)
            if not request_id:
                continue
            current = str(item.get("status") or "")
            if current in {"rejected", "cancelled", "unsupported"}:
                continue
            if current != job_status:
                db.guest_request_commands.add_event(
                    request_id,
                    "info",
                    "系统同步关联正式任务状态",
                    {"job_id": job_id, "job_status": job_status, "previous_status": current, **(raw or {})},
                )
            db.guest_request_commands.update(
                request_id,
                status=job_status,
                public_status=public_status,
                raw_data=_merge_raw_data(item.get("raw_data"), {"status_sync": {"job_id": job_id, "job_status": job_status, **(raw or {})}}),
            )

    def _no_job_reconcile(job: dict[str, Any], reason: str) -> dict[str, Any]:
        del reason
        return job

    def _effective_settings() -> dict[str, Any]:
        public_defaults = app_config.raw.get("public", {})
        stored = db.app_settings.get_all()
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
            db.app_settings.get_all()
            database_status = {"healthy": True}
        except Exception as exc:  # noqa: BLE001
            database_status = {"healthy": False, "error": str(exc)}
        try:
            usage = shutil.disk_usage(database.path.parent)
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
            key: db.organizer.count_organizer_tasks(status=key)
            for key in ("failed", "executing", "waiting_openlist", "stabilizing", "pending")
        }
        return status

    def _thread_component(thread: Any, *, required: bool = True) -> dict[str, Any]:
        running = bool(thread and thread.is_alive())
        return {"required": bool(required), "running": running, "healthy": running or not required}

    def _local_worker_system_status() -> dict[str, Any]:
        data_status = _admin_data_status()
        database_healthy = bool((data_status.get("database") or {}).get("healthy"))
        rclone_status = rclone_service.status()
        organizer_status = _admin_organizer_status()
        update_status = update_scheduler.status()
        trending_status = trending_scheduler.status()
        queue_status = worker_queue_diagnostics.status()
        checks: dict[str, dict[str, Any]] = {
            "database": {"required": True, "running": database_healthy, "healthy": database_healthy}
        }
        if role_runs(active_process_role, "worker"):
            checks.update(
                {
                    "durable_worker": {
                        "required": True,
                        "running": bool(queue_status.get("runtime_running")),
                        "healthy": bool(queue_status.get("healthy")),
                    },
                    "search_cache_maintenance": _thread_component(search_cache_maintenance_worker.thread),
                    "event_retention": _thread_component(event_retention_worker.thread),
                }
            )
        if role_runs(active_process_role, "scheduler"):
            checks.update(
                {
                    "update_scheduler": _thread_component(
                        update_scheduler.thread,
                        required=bool(update_status.get("enabled")),
                    ),
                    "trending_discovery": _thread_component(
                        trending_scheduler.thread,
                        required=bool(trending_status.get("enabled")),
                    ),
                    "notification_digest": _thread_component(notification_digest_scheduler.thread),
                }
            )
        failed_checks = [name for name, item in checks.items() if not item.get("healthy")]
        healthy = not failed_checks
        return {
            "success": True,
            "healthy": healthy,
            "status": "ready" if healthy else "unhealthy",
            "process_role": active_process_role,
            "failed_checks": failed_checks,
            "checks": checks,
            "rclone": rclone_status,
            "organizer": organizer_status,
            "update_scheduler": update_status,
            "trending_discovery": trending_status,
            "worker_queue": queue_status,
            "data": data_status,
            **({"message": "Worker 核心组件异常：" + "、".join(failed_checks)} if failed_checks else {}),
        }

    def _admin_system_status() -> dict[str, Any]:
        data_status = _admin_data_status()
        database_healthy = bool((data_status.get("database") or {}).get("healthy"))
        if rclone_worker_client is not None:
            worker_status = rclone_worker_client.system_status()
            if worker_status.get("success"):
                return {
                    "ok": database_healthy and bool(worker_status.get("healthy")),
                    "database": "ok" if database_healthy else "error",
                    "rclone": worker_status.get("rclone") or {},
                    "organizer": worker_status.get("organizer") or {},
                    "update_scheduler": worker_status.get("update_scheduler") or {},
                    "trending_discovery": worker_status.get("trending_discovery") or {},
                    "worker_queue": worker_status.get("worker_queue") or {},
                    "worker": worker_status,
                    "data": data_status,
                }
            unavailable = {
                "healthy": False,
                "status": str(worker_status.get("status") or "worker_unavailable"),
                "message": str(worker_status.get("message") or "rclone Worker 不可用"),
            }
            return {
                "ok": False,
                "database": "ok" if database_healthy else "error",
                "rclone": {**unavailable, "running": False},
                "organizer": unavailable,
                "update_scheduler": unavailable,
                "trending_discovery": unavailable,
                "worker_queue": unavailable,
                "worker": worker_status,
                "data": data_status,
            }
        local_status = _local_worker_system_status()
        return {
            "ok": database_healthy and bool(local_status.get("healthy")),
            "database": "ok" if database_healthy else "error",
            "rclone": local_status["rclone"],
            "organizer": local_status["organizer"],
            "update_scheduler": local_status["update_scheduler"],
            "trending_discovery": local_status["trending_discovery"],
            "worker_queue": local_status["worker_queue"],
            "worker": local_status,
            "data": data_status,
        }

    # V1 readiness is based on the already-composed database-owned graph and
    # the single FNOS_ROOT_KEY.

    def _readiness_root_key() -> bool:
        try:
            return bool(master_key_from_env())
        except Exception:  # noqa: BLE001 - readiness must not expose key details
            return False

    def _readiness_active_provider_count() -> int:
        service = v1_accounts_service
        if service is not None:
            count = 0
            for selection in service.list_active_selections().values():
                account = service.get_account(str(selection.get("account_id") or ""))
                if account and account.enabled and str(account.last_test_status or "").lower() == "ok":
                    count += 1
            return count
        with db.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                  FROM provider_account_selections s
                  JOIN provider_accounts a ON a.account_id=s.account_id
                 WHERE a.enabled=1 AND lower(a.last_test_status)='ok'
                """
            ).fetchone()
        return int(row["count"] or 0) if row is not None else 0

    def _readiness_openlist_required() -> bool:
        organizer = app_config.raw.get("organizer", {}) if isinstance(app_config.raw.get("organizer"), dict) else {}
        if bool(organizer.get("enabled")):
            return True
        # A cross-Provider rclone/relay route needs a readable source landing
        # mount even when Organizer is disabled. ``storage_targets.account_scope``
        # stores a scope such as ``default`` rather than an account id, so it
        # must not be joined to provider_account_selections.account_id.
        return routes_require_openlist(
            v1_rules_service.list_assignments(),
            v1_rules_service.sources(),
            v1_target_service.get_target,
        )

    def _readiness_valid_mount_count() -> int:
        with db.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM openlist_mounts
                 WHERE enabled=1 AND readable=1
                   AND identity_status IN ('verified','declared')
                """
            ).fetchone()
        return int(row["count"] or 0) if row is not None else 0

    def _readiness_enabled_target_count() -> int:
        with db.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM storage_targets WHERE enabled=1"
            ).fetchone()
        return int(row["count"] or 0) if row is not None else 0

    readiness_service = ReadinessService(
        process_role=active_process_role,
        database_probe=lambda: db.app_settings.get_all(),
        docker_socket_mounted=lambda: Path("/var/run/docker.sock").exists(),
        remote_worker_status=(
            rclone_worker_client.system_status if rclone_worker_client is not None else None
        ),
        local_worker_status=_local_worker_system_status,
        root_key_probe=_readiness_root_key,
        active_provider_accounts_probe=_readiness_active_provider_count,
        openlist_required=_readiness_openlist_required,
        valid_openlist_mounts_probe=_readiness_valid_mount_count,
        enabled_storage_targets_probe=_readiness_enabled_target_count,
    )
    app.extensions["readiness_service"] = readiness_service

    worker_watchdog: WorkerWatchdog | None = None
    if (
        str(os.getenv("APP_ENV") or "").strip().lower() == "production"
        and role_runs(active_process_role, "worker")
    ):
        worker_watchdog = WorkerWatchdog(
            status=_local_worker_system_status,
            terminate=lambda: os._exit(1),
            log_critical=app.logger.critical,
        )
        app.extensions["worker_watchdog"] = worker_watchdog
        atexit.register(worker_watchdog.shutdown)

    def api_admin_dashboard():
        service = AdminDashboardService(
            AdminDashboardDependencies(
                jobs=db.job_queries,
                requests=db.guest_request_queries,
                reconcile_job=_no_job_reconcile,
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
                db=notification_repositories,
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
                db=notification_repositories,
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
        return jsonify({"success": True, "message": "已拒绝提交", "request": db.guest_request_queries.get(request_id), "job_cancel": cancel_result, "worker_cancel": request_worker_cancel})

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
        return jsonify({"success": True, "message": "已取消提交", "request": db.guest_request_queries.get(request_id), "job_cancel": cancel_result, "worker_cancel": request_worker_cancel})

    def api_admin_jobs():
        page, per_page, offset = _page_args(20, 200)
        status = request.args.get("status") or None
        category = request.args.get("category") or None
        source_provider_kind = request.args.get("source_provider_kind") or None
        keyword = request.args.get("keyword") or request.args.get("q") or None
        result = _job_admin_query_application().list_jobs(
            limit=per_page,
            offset=offset,
            status=status,
            category=category,
            source_provider_kind=source_provider_kind,
            keyword=keyword,
        )
        return jsonify({"success": True, "items": result["items"], "pagination": _page_meta(result["total"], page, per_page)})

    def _job_admin_query_application() -> JobAdminQueryService:
        return JobAdminQueryService(
            JobAdminQueryDependencies(
                jobs=db.job_queries,
                load_detail=job_service.get_job_with_events,
                reconcile=_no_job_reconcile,
                decorate=_decorate_job_completion,
                load_transfer_execution=v1_execution_repository.get,
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
                job_queries=db.job_queries,
                job_commands=db.job_commands,
                auto_start_rclone=_auto_start_rclone_for_import,
            )
        )

    def api_admin_job_cancel(job_id: int):
        job = db.job_queries.get(job_id)
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
                "job": result.get("job") or db.job_queries.get(job_id),
                "cleanup": result.get("cleanup"),
            }), 409
        for guest_request in db.guest_request_queries.list_by_job(job_id):
            request_id = guest_request.get("id")
            if not request_id:
                continue
            db.guest_request_commands.add_event(int(request_id), "warn", f"关联任务已取消：{reason}", {"job_id": job_id})
            db.guest_request_commands.update(
                int(request_id),
                status=JOB_CANCELLED,
                public_status="已取消",
                raw_data=_merge_raw_data(guest_request.get("raw_data"), {"cancelled_by": session.get("admin_username"), "reason": reason, "job_id": job_id}),
            )
        return jsonify({"success": True, "message": result.get("message") or "已取消任务", "job": db.job_queries.get(job_id), "cleanup": result.get("cleanup")})

    def _job_cancellation_service() -> JobCancellationService:
        return JobCancellationService(
            JobCancellationDependencies(
                job_queries=db.job_queries,
                job_commands=db.job_commands,
                cleaner=rclone_controller,
                merge_raw_data=_merge_raw_data,
                payload_bool=_payload_bool,
                cancelled_status=JOB_CANCELLED,
                worker_tasks=db.worker_tasks,
                organizer=organizer_service,
                execution_repository=getattr(
                    app.extensions.get("v1_transfer_dispatch"),
                    "executions",
                    None,
                ),
                enqueue_transfer=worker_task_dispatcher.import_transfer_execute,
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

    def _rclone_proxy_response(result: dict[str, Any]):
        payload = dict(result)
        status_code = _safe_int_value(payload.pop("_http_status", 200), 200)
        return jsonify(payload), status_code

    def api_admin_rclone_status():
        return _rclone_proxy_response(_rclone_admin_query_application().status())

    def api_admin_rclone_start():
        payload = request.get_json(silent=True) or {}
        return _rclone_proxy_response(_rclone_admin_command_application().start(payload))

    def api_admin_rclone_stop():
        return _rclone_proxy_response(_rclone_admin_command_application().stop())

    def api_admin_rclone_logs():
        limit = _safe_int(request.args.get("limit"), 200, 1, 1000)
        return _rclone_proxy_response(_rclone_admin_query_application().logs(limit))

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
        return RcloneAdminQueryService(
            RcloneAdminQueryDependencies(
                rclone=rclone_controller,
                counts=db.rclone,
            )
        )

    def api_admin_rclone_file_retry(event_id: int):
        payload = request.get_json(silent=True) or {}
        service = RcloneFileRetryService(RcloneFileRetryDependencies(database=db, runner=rclone_controller))
        result, status_code = service.retry(event_id, force=bool(payload.get("force")))
        return jsonify(result), status_code

    def api_admin_rclone_check():
        return _rclone_proxy_response(_rclone_admin_command_application().check())

    def _rclone_webdav_response(action: Callable[[], dict[str, Any]]):
        try:
            result = action()
        except RcloneWebdavConfigError as exc:
            return jsonify({"success": False, "message": str(exc)}), exc.status_code
        except RcloneWorkerRequestError as exc:
            payload = dict(exc.payload)
            payload.pop("_http_status", None)
            return jsonify(payload or {"success": False, "message": str(exc)}), exc.status_code
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store, private, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response, 200

    def api_admin_rclone_webdav_config():
        return _rclone_webdav_response(
            lambda: (
                rclone_worker_client.webdav_status(request.args.get("remote_name"))
                if rclone_worker_client is not None
                else rclone_webdav_config_service.status(request.args.get("remote_name"))
            )
        )

    def api_admin_rclone_webdav_config_update():
        payload = request.get_json(silent=True)
        return _rclone_webdav_response(
            lambda: (
                rclone_worker_client.webdav_save({} if payload is None else payload)
                if rclone_worker_client is not None
                else rclone_webdav_config_service.save({} if payload is None else payload)
            )
        )

    def api_admin_rclone_webdav_config_test():
        payload = request.get_json(silent=True) or {}
        return _rclone_webdav_response(
            lambda: (
                rclone_worker_client.webdav_test(payload.get("remote_name"))
                if rclone_worker_client is not None
                else rclone_webdav_config_service.test(payload.get("remote_name"))
            )
        )

    def _rclone_admin_command_application() -> RcloneAdminCommandService:
        return RcloneAdminCommandService(RcloneAdminCommandDependencies(rclone=rclone_controller))

    def _rclone_worker_payload() -> dict[str, Any]:
        payload = request.get_json(silent=True)
        return payload if isinstance(payload, dict) else {}

    def _rclone_worker_webdav_response(action: Callable[[], dict[str, Any]]):
        try:
            return jsonify(action())
        except RcloneWebdavConfigError as exc:
            return jsonify({"success": False, "message": str(exc)}), exc.status_code

    def _rclone_worker_cancel_job():
        payload = _rclone_worker_payload()
        job_id = _safe_int_value(payload.get("job_id"), 0)
        if job_id <= 0 or job_id > 999999999:
            return jsonify({"success": False, "message": "缺少有效的 rclone Job ID"}), 400
        return jsonify(
            rclone_service.cancel_job(
                job_id,
                stop_running=bool(payload.get("stop_running")),
            )
        )

    def _rclone_worker_control_handlers() -> dict[str, Callable[..., Any]]:
        return {
            "worker_status": lambda: jsonify(_local_worker_system_status()),
            "status": lambda: jsonify({"success": True, "status": rclone_service.status()}),
            "logs": lambda: jsonify(
                {
                    "success": True,
                    "items": rclone_service.get_logs(
                        limit=_safe_int(request.args.get("limit"), 200, 1, 1000)
                    ),
                }
            ),
            "start": lambda: jsonify(
                rclone_service.start(
                    reason=str(_rclone_worker_payload().get("reason") or "remote_control"),
                    file_retry=_rclone_worker_payload().get("file_retry"),
                    category_filter=str(_rclone_worker_payload().get("category_filter") or ""),
                    staging_run=_rclone_worker_payload().get("staging_run"),
                )
            ),
            "stop": lambda: jsonify(rclone_service.stop()),
            "check": lambda: jsonify(rclone_service.check_environment()),
            "cancel_job": _rclone_worker_cancel_job,
            "cleanup_cancelled_task": lambda: jsonify(
                rclone_service.cleanup_cancelled_task(**_rclone_worker_payload())
            ),
            "file_retry": lambda: jsonify(
                rclone_service.start_file_retry(
                    _rclone_worker_payload().get("file_event")
                    if isinstance(_rclone_worker_payload().get("file_event"), dict)
                    else {}
                )
            ),
            "runtime_reload": lambda: jsonify(_reload_runtime_config()),
            "webdav_config": lambda: _rclone_worker_webdav_response(
                lambda: rclone_webdav_config_service.status(request.args.get("remote_name"))
            ),
            "webdav_config_update": lambda: _rclone_worker_webdav_response(
                lambda: rclone_webdav_config_service.save(_rclone_worker_payload())
            ),
            "webdav_config_test": lambda: _rclone_worker_webdav_response(
                lambda: rclone_webdav_config_service.test(
                    _rclone_worker_payload().get("remote_name")
                )
            ),
        }

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
        return OrganizerAdminQueryService(
            OrganizerAdminQueryDependencies(
                organizer=organizer_service,
                counts=db.organizer,
            )
        )

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
        runs = db.update.list_update_runs(subscription_id=subscription_id, limit=3)
        candidates = update_service.filter_display_candidates(db.update.list_update_candidates(subscription_id=subscription_id, limit=100))[:30]
        events = db.update.list_update_events(subscription_id=subscription_id, limit=20)
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
        snapshot = db.update.get_update_path_snapshot(subscription_id, root) if root else None
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
        items = db.update.list_update_runs(subscription_id=subscription_id, limit=per_page, offset=offset)
        total = db.update.count_update_runs(subscription_id=subscription_id)
        return jsonify({"success": True, "items": items, "pagination": _page_meta(total, page, per_page)})

    def api_admin_update_run_detail(run_id: int):
        item = db.update.get_update_run(run_id)
        if not item:
            return jsonify({"success": False, "message": "追更运行记录不存在"}), 404
        events = db.update.list_update_events(run_id=run_id, limit=100)
        candidates = update_service.filter_display_candidates(db.update.list_update_candidates(run_id=run_id, limit=200))[:100]
        return jsonify({"success": True, "item": item, "events": events, "candidates": candidates})

    def api_admin_update_candidates():
        page, per_page, offset = _page_args(50, 200)
        subscription_id = _safe_int(request.args.get("subscription_id"), 0, 0, 999999999) or None
        run_id = _safe_int(request.args.get("run_id"), 0, 0, 999999999) or None
        decision = request.args.get("decision") or None
        items = db.update.list_update_candidates(subscription_id=subscription_id, run_id=run_id, decision=decision, limit=per_page, offset=offset)
        total = db.update.count_update_candidates(subscription_id=subscription_id, run_id=run_id, decision=decision)
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
        pending_candidates = db.trending.list_candidates(status="importing", limit=200, offset=0)
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
            "discovered": db.trending.count_candidates(status="discovered"),
            "already_exists": db.trending.count_candidates(status="already_exists"),
            "task_exists": db.trending.count_candidates(status="task_exists"),
            "ignored": db.trending.count_candidates(status="ignored"),
            "importing": db.trending.count_candidates(status="importing"),
            "imported": db.trending.count_candidates(status="imported"),
            "import_failed": db.trending.count_candidates(status="import_failed"),
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
        items = db.trending.list_runs(limit=per_page, offset=offset)
        total = db.trending.count_runs()
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
        item = db.trending.get_candidate(candidate_id)
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
                keyword=_limited_text(payload.get("keyword"), "搜索关键词", 300),
            )
            return jsonify(result)
        except (TrendingInitialImportError, PublicInputError) as exc:
            return jsonify({"success": False, "message": str(exc)}), getattr(exc, "status_code", 400)
        except Exception:  # noqa: BLE001
            app.logger.exception("trending candidate search failed: candidate_id=%s", candidate_id)
            return jsonify({"success": False, "message": "搜索资源失败"}), 502

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
            fid = _limited_text(request.args.get("fid"), "目录 ID", 256, required=True)
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
            return jsonify({"success": False, "message": "创建热榜首次入库任务失败"}), 500

    def api_admin_trending_candidate_ignore(candidate_id: int):
        item = db.trending.get_candidate(candidate_id)
        if not item:
            return jsonify({"success": False, "message": "热榜候选不存在"}), 404
        if str(item.get("status") or "") == "importing":
            return jsonify({"success": False, "message": "首入库任务正在执行，暂不能忽略该候选"}), 409
        payload = request.get_json(silent=True) or {}
        db.trending.update_candidate(
            candidate_id,
            status="ignored",
            ignore_reason=str(payload.get("reason") or "管理员忽略"),
        )
        return jsonify({"success": True, "item": db.trending.get_candidate(candidate_id), "message": "热榜候选已忽略"})

    def api_admin_trending_candidate_restore(candidate_id: int):
        item = db.trending.get_candidate(candidate_id)
        if not item:
            return jsonify({"success": False, "message": "热榜候选不存在"}), 404
        media_exists = _hot_media_exists(item)
        status = "already_exists" if media_exists else "task_exists" if _hot_task_exists(item) else "discovered"
        db.trending.update_candidate(
            candidate_id,
            status=status,
            media_exists=media_exists,
            ignore_reason=None,
            ignore_until=None,
        )
        return jsonify({"success": True, "item": db.trending.get_candidate(candidate_id), "message": "热榜候选已恢复"})

    def api_admin_trending_candidate_subscribe(candidate_id: int):
        item = db.trending.get_candidate(candidate_id)
        if not item:
            return jsonify({"success": False, "message": "热榜候选不存在"}), 404
        # 持久绑定必须以 TMDB/category/season 为身份。标题匹配只用于
        # discovery 展示 task_exists，不能在这里直接绑定同名翻拍或不同季。
        result, status_code = hot_subscription.create_from_candidate(
            item,
            update_service.tmdb,
            lambda payload: update_service.create_subscription_from_trending_candidate(candidate_id, payload),
        )
        result["item"] = db.trending.get_candidate(candidate_id)
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

    settings_service = SettingsService(
        SettingsDependencies(
            db=db,
            raw_config=lambda: app_config.raw,
            redact_config=_redact_config,
            advanced_response=advanced_config_response,
            normalize_advanced=normalize_advanced_config_payload,
            advanced_key=ADVANCED_CONFIG_KEY,
            reload_runtime=_reload_runtime_config,
            effective_settings=_effective_settings,
            payload_bool=_payload_bool,
            search_providers=search_service.describe_providers,
        )
    )
    from .accounts.service import ProviderAccountService
    from .accounts.blueprint import accounts_blueprint_for_service
    from .accounts.auth_sessions import (
        ManagedAccountAuthHandler,
        ProviderAuthSessionService,
        QuarkQrAuthHandler,
        SixpanDeviceCodeAuthHandler,
        TianyiPasswordAuthHandler,
    )
    from .openlist.connection_service import OpenListConnectionService
    from .openlist.mount_service import OpenListMountService
    from .openlist.blueprint import OpenListRouteContext, create_openlist_blueprint, default_handlers
    from .storage.target_service import StorageTargetService
    from .storage.assignment_service import SourceAssignmentService
    from .storage.blueprint import StorageBlueprintContext, create_storage_blueprint
    from .storage.setup_blueprint import StorageSetupBlueprintContext, create_storage_setup_blueprint
    from .storage.setup_service import StorageSetupService
    from .services.source_record_service import SourceRecordService

    def _openlist_api_client_from_credentials(credentials: dict[str, Any]) -> Any:
        """Build a JSON-API OpenList client from stored WebDAV credentials.

        The stored ``base_url`` is a WebDAV endpoint (``.../dav``); the fs APIs
        live on the service root, so reuse the Organizer's conversion instead of
        re-deriving it here.
        """
        options = (
            app_config.raw.get("openlist", {})
            if isinstance(app_config.raw.get("openlist"), dict)
            else {}
        )
        config = {**dict(options), **dict(credentials or {})}
        config["base_url"] = openlist_api_base_url(config.get("base_url"))
        return OpenListClient(config)

    app.extensions["v1_accounts_service"] = v1_accounts_service
    app.extensions["v1_openlist_connections"] = v1_openlist_connections
    app.extensions["v1_openlist_mounts"] = v1_openlist_mounts
    app.extensions["v1_target_service"] = v1_target_service
    app.extensions["v1_rules_service"] = v1_rules_service
    app.extensions["v1_source_record_service"] = v1_source_record_service

    v1_storage_setup_service = StorageSetupService(
        accounts_service=v1_accounts_service,
        connection_service=v1_openlist_connections,
        mount_service=v1_openlist_mounts,
        target_service=v1_target_service,
        assignment_service=v1_rules_service,
        connection_factory=db.connect,
        rclone_webdav_sync=lambda payload: (
            rclone_worker_client.webdav_save(dict(payload))
            if rclone_worker_client is not None
            else rclone_webdav_config_service.save(dict(payload))
        ),
        rclone_webdav_test=lambda: (
            rclone_worker_client.webdav_test()
            if rclone_worker_client is not None
            else rclone_webdav_config_service.test()
        ),
        organizer_enabled=lambda: bool(
            (
                app_config.raw.get("organizer", {})
                if isinstance(app_config.raw.get("organizer"), dict)
                else {}
            ).get("enabled", False)
        ),
        openlist_api_factory=_openlist_api_client_from_credentials,
    )
    app.extensions["v1_storage_setup_service"] = v1_storage_setup_service

    def _resolve_update_destination(requested: Any) -> str | None:
        """Validate a subscription destination against the V1 DB graph.

        Update subscriptions may pin a destination identity, but they may not
        read the removed YAML storage registry or category-specific provider
        paths.  A target is usable only when its current revision and binding
        are both enabled and resolvable; the immutable import plan will freeze
        the exact revision again when an episode is submitted.
        """

        destination_id = str(requested or "").strip()
        if not destination_id:
            return None
        target = v1_target_service.get_target(destination_id)
        if target is None or not bool(target.enabled):
            return None
        try:
            binding = v1_target_service.resolve_binding(
                destination_id,
                int(target.current_revision),
            )
        except Exception:  # noqa: BLE001
            return None
        if not bool(getattr(binding, "enabled", True)):
            return None
        account_id = str(getattr(binding, "account_id", "") or "").strip()
        if account_id:
            account = v1_accounts_service.get_account(account_id)
            if account is None or not bool(getattr(account, "enabled", False)):
                return None
            if str(getattr(account, "last_test_status", "") or "") != "ok":
                return None
            if not v1_accounts_service.has_revision(
                account_id,
                int(getattr(binding, "account_revision", 0) or 0),
            ):
                return None
        return destination_id

    update_service.set_destination_resolver(_resolve_update_destination)

    def _resolve_update_openlist_session(
        subscription: dict[str, Any],
        category_key: str,
    ) -> dict[str, Any] | None:
        """Resolve an update scan client/root from the V1 target graph."""

        raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
        destination_id = str(raw_data.get("destination_id") or subscription.get("destination_id") or "").strip()
        if not destination_id or not _resolve_update_destination(destination_id):
            return None
        target = v1_target_service.get_target(destination_id)
        if target is None or not bool(target.enabled):
            return None
        category = target.category(str(category_key or "").strip())
        locator = getattr(category, "organizer_locator", None) if category is not None else None
        if locator is None:
            return None
        mount_id = str(getattr(locator, "mount_id", "") or "").strip()
        connection_id = str(getattr(locator, "connection_id", "") or "").strip()
        if not mount_id or not connection_id:
            return None
        mount = v1_openlist_mounts.get_mount(mount_id)
        connection = v1_openlist_connections.get_connection(connection_id)
        if not isinstance(mount, dict) or not isinstance(connection, dict):
            return None
        if str(mount.get("connection_id") or "") != connection_id:
            return None
        if not bool(mount.get("enabled")) or not bool(mount.get("readable")):
            return None
        if str(mount.get("identity_status") or "").strip().lower() not in {"verified", "declared"}:
            return None
        if not bool(connection.get("enabled")) or not bool(connection.get("credential_configured")):
            return None
        if str(connection.get("last_test_status") or "").strip().lower() != "ok":
            return None
        try:
            credentials = v1_openlist_connections.resolve_credentials(connection_id)
        except Exception:  # noqa: BLE001 - fail closed when the root key/credentials are unavailable
            return None
        options = app_config.raw.get("openlist", {}) if isinstance(app_config.raw.get("openlist"), dict) else {}
        client = OpenListClient({**dict(options), **credentials})
        mount_root = str(mount.get("webdav_root") or "").strip().replace("\\", "/")
        relative_root = str(getattr(locator, "path", "") or "").strip().replace("\\", "/")
        category_root = posixpath.normpath(
            "/" + "/".join(part.strip("/") for part in (mount_root, relative_root) if part.strip("/"))
        )
        if category_root in {"", "/", "/.", "/.."}:
            return None
        return {
            "client": client,
            "category_root": category_root,
            "destination_id": destination_id,
            "mount_id": mount_id,
            "connection_id": connection_id,
            "namespace_id": str(mount.get("namespace_id") or ""),
            "target_revision": int(target.current_revision),
        }

    update_service.set_openlist_session_resolver(_resolve_update_openlist_session)

    def _tianyi_auth_importer(config: dict[str, Any]) -> Any:
        from .importers.tianyi import TianyiImporter

        return TianyiImporter(dict(config or {}))

    def _active_tianyi_importer() -> Any:
        return generic_importers["cloud189"]

    def _provider_supports_relay_upload(provider_kind: str) -> bool:
        try:
            adapter = v1_adapter_registry.get(str(provider_kind or "").strip().lower())
            capabilities = adapter.capabilities()
        except Exception:  # noqa: BLE001 - capability policy must fail closed
            return False
        if isinstance(capabilities, dict):
            return bool(capabilities.get("supports_relay_upload"))
        return bool(getattr(capabilities, "supports_relay_upload", False))

    def _storage_provider_uploader(
        provider_kind: str,
        account_id: str,
        revision: int,
    ) -> Any:
        adapter = v1_adapter_registry.get(str(provider_kind or "").strip().lower())
        factory = getattr(adapter, "relay_uploader_for", None)
        if not callable(factory):
            return None
        return factory(str(account_id or "").strip(), int(revision or 0))

    def _storage_provider_folder_browser(
        provider_kind: str,
        account_id: str,
        revision: int,
    ) -> Any:
        adapter = v1_adapter_registry.get(str(provider_kind or "").strip().lower())
        factory = getattr(adapter, "folder_browser_for", None)
        if not callable(factory):
            return None
        return factory(str(account_id or "").strip(), int(revision or 0))

    # Storage health is Provider-capability driven.  A relay-capable target may
    # use read-only WebDAV for visibility/Organizer operations while its bytes
    # are uploaded through the frozen Provider account's official API.
    v1_openlist_mounts.set_relay_upload_capability_resolver(
        _provider_supports_relay_upload
    )
    v1_storage_setup_service.set_provider_uploader_resolver(
        _storage_provider_uploader
    )
    v1_storage_setup_service.set_provider_folder_resolver(
        _storage_provider_folder_browser
    )
    app.extensions["v1_provider_registry"] = v1_adapter_registry
    auth_handlers: dict[tuple[str, str], Any] = {
        ("quark", "qrcode"): QuarkQrAuthHandler(v1_adapter_registry.account_tester),
        ("tianyi", "password"): TianyiPasswordAuthHandler(_tianyi_auth_importer),
        ("mobile", "managed_account"): ManagedAccountAuthHandler("mobile", lambda: {}),
        ("sixpan", "device_code"): SixpanDeviceCodeAuthHandler(
            lambda: {},
            lambda config: SixPanOfflineImporter(config),
        ),
    }
    provider_auth_sessions = ProviderAuthSessionService(
        db,
        _v1_master_key,
        v1_accounts_service,
        auth_handlers,
    )
    app.extensions["provider_auth_sessions"] = provider_auth_sessions

    from .transfer.dispatch import TransferDispatchService
    from .openlist.locator_resolver import RcloneLocatorResolver

    locator_config = rclone_runtime_config(app_config)
    locator_config["relay_root"] = os.getenv("FNOS_RELAY_ROOT", "/data/relay")
    v1_rclone_locator = RcloneLocatorResolver(locator_config)
    v1_rclone_locator.bind_revision_services(
        v1_openlist_mounts,
        v1_openlist_connections,
    )
    rclone_service.set_v1_locator_resolver(
        v1_rclone_locator,
        mount_service=v1_openlist_mounts,
        connection_service=v1_openlist_connections,
    )
    v1_transfer_dispatch = TransferDispatchService(
        database=db,
        accounts_service=v1_accounts_service,
        target_service=v1_target_service,
        mount_service=v1_openlist_mounts,
        registry=v1_adapter_registry,
        rclone_service=rclone_service,
        locator_resolver=v1_rclone_locator,
        source_record_service=v1_source_record_service,
        assignment_service=v1_rules_service,
        execution_repository=v1_execution_repository,
        organizer_service=organizer_service,
    )
    app.extensions["v1_rclone_locator"] = v1_rclone_locator
    app.extensions["v1_transfer_dispatch"] = v1_transfer_dispatch
    runtime_installer = RuntimeInstaller(
        locator_resolver=v1_rclone_locator,
        transfer_dispatch=v1_transfer_dispatch,
    )
    runtime_installer.install(runtime_build, app_config)

    transfer_recovery_scanner = TransferRecoveryScanner(
        v1_transfer_dispatch.executions,
        db.worker_tasks,
        max_attempts=max(
            1,
            _config_int(
                app_config.raw.get("worker", {})
                if isinstance(app_config.raw.get("worker"), dict)
                else {},
                "transfer_max_attempts",
                20,
            ),
        ),
    )
    transfer_recovery_runtime = TransferRecoveryRuntime(
        transfer_recovery_scanner,
        interval_seconds=max(
            5,
            _config_int(
                app_config.raw.get("worker", {})
                if isinstance(app_config.raw.get("worker"), dict)
                else {},
                "transfer_recovery_interval_seconds",
                30,
            ),
        ),
        batch_size=max(
            1,
            _config_int(
                app_config.raw.get("worker", {})
                if isinstance(app_config.raw.get("worker"), dict)
                else {},
                "transfer_recovery_batch_size",
                100,
            ),
        ),
        log=logging.getLogger("fnos_media_import.transfer_recovery").warning,
    )
    app.extensions["transfer_recovery_scanner"] = transfer_recovery_scanner
    app.extensions["transfer_recovery_runtime"] = transfer_recovery_runtime
    atexit.register(transfer_recovery_runtime.shutdown)

    # The durable runtime is created earlier so all process roles share one
    # queue object; registering the handler now is safe because it is started
    # only after the application has finished composing every V1 dependency.
    durable_worker_runtime.register(
        "import_transfer_execute",
        v1_transfer_dispatch.handle_worker,
    )

    notification_settings_service = NotificationSettingsService(
        db=notification_repositories,
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
        settings = db.app_settings.get_all()
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
        current = db.app_settings.get_all().get("search.providers")
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

        db.app_settings.set_many({"search.providers": provider_settings})
        return jsonify({"success": True, "message": "搜索源设置已保存", "items": search_service.describe_providers()})

    def api_admin_search_aliases():
        aliases = db.app_settings.get_all().get("search.aliases") or {}
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
        db.app_settings.set_many({"search.aliases": normalized_aliases})
        return jsonify({"success": True, "message": "搜索别名词库已保存", "aliases": normalized_aliases})

    def api_admin_adapters():
        return jsonify(
            {
                "success": True,
                "items": _adapter_placeholders(
                    app_config.raw,
                    generic_importers,
                    cloud139_importer=cloud139_importer,
                    quark_importer=quark_importer,
                ),
            }
        )

    def api_admin_adapter_probe(adapter_key: str):
        adapters = {
            item["key"]: item
            for item in _adapter_placeholders(
                app_config.raw,
                generic_importers,
                cloud139_importer=cloud139_importer,
                quark_importer=quark_importer,
            )
        }
        item = adapters.get(adapter_key)
        if not item:
            return jsonify({"success": False, "message": "线路适配器不存在"}), 404
        if adapter_key == "quark":
            importer = quark_importer
        elif adapter_key == "cloud139":
            importer = cloud139_importer
        elif adapter_key == "cloud189":
            importer = _active_tianyi_importer()
        else:
            importer = generic_importers.get(adapter_key)
        probe = get_optional_provider_method(importer, "probe")
        if probe:
            result = probe()
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
                "category_labels": CATEGORY_LABELS,
                "security": _public_security_config(security_config),
                "public": settings["public"],
                "submission": settings["submission"],
                "search": {
                    "providers": search_service.describe_providers(),
                    "result_provider_badges": _public_result_provider_badges(
                        app_config.raw.get("pansou")
                    ),
                },
                "adapters": _public_adapter_capabilities(
                    app_config.raw,
                    quark_importer=quark_importer,
                    cloud139_importer=cloud139_importer,
                    generic_importers={
                        **generic_importers,
                        "cloud189": _active_tianyi_importer(),
                    },
                ),
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
                "availability_text": "已经可以观看了" if media_exists else "搜索资源",
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

    def api_public_tmdb_search():
        if not _current_public_settings().get("allow_anonymous_search", True):
            return jsonify({"success": False, "message": "匿名搜索已关闭，请使用链接提交。"}), 403
        limited = _rate_limit("public_tmdb_search", "public_tmdb_search_rate_limit")
        if limited:
            return limited
        try:
            keyword = _limited_text(
                request.args.get("q") or request.args.get("query"),
                "影视关键词",
                _config_int(security_config, "max_keyword_length", 80),
                required=True,
            )
        except PublicInputError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        result = public_tmdb_service.search(keyword)
        return jsonify({"success": True, **result})

    def _public_submission_application() -> PublicSubmissionService:
        return PublicSubmissionService(
            PublicSubmissionDependencies(
                queries=db.guest_request_queries,
                commands=db.guest_request_commands,
                sync_request=_sync_guest_request_status,
                public_status=_public_status,
                public_request=_public_request_response,
                db=notification_repositories,
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
                cache_queries=db.search_cache_queries,
                cache_commands=db.search_cache_commands,
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
        try:
            result = _public_search_application().search(
                keyword,
                sources=sources,
                token=token,
                options=search_options,
                hide_full_links=public_settings.get("hide_full_links", True),
                trace_id=trace_id,
            )
        except SearchProvidersUnavailableError as exc:
            app.logger.warning(
                "search_trace=%s stage=public_route_unavailable error=%s",
                trace_id,
                str(exc)[:500],
            )
            return jsonify(
                {
                    "success": False,
                    "code": "SEARCH_PROVIDERS_UNAVAILABLE",
                    "message": "搜索源暂不可用，请稍后重试",
                    "retryable": True,
                    "trace_id": trace_id,
                    "items": [],
                }
            ), 503
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
        link = detect_link(url, password=password)
        tianyi = _active_tianyi_importer()
        public_link = {
            "provider_kind": link.provider_kind,
            "source_form": link.source_form,
            "search_source_id": link.search_source_id,
            "supported": link.supported,
            "reason": "已识别资源类型，提交时会检测可用性" if link.supported else "当前资源暂不支持自动入库",
            "detail_capability": _detail_capability_for_source(
                link.provider_kind,
                cloud139_importer=cloud139_importer,
                sixpan_importer=generic_importers.get("sixpan"),
                tianyi_importer=tianyi,
            ),
        }
        return jsonify({"success": True, "link": public_link})

    public_manual_preview_service = PublicManualPreviewService(
        cache_queries=db.search_cache_queries,
        cache_commands=db.search_cache_commands,
        new_public_id=_new_public_id,
        detect_link=detect_link,
        resource_detail=_public_resource_detail,
        present_item=_public_resource_item,
        detail_capability=_detail_capability_for_source,
        quark_importer=lambda: quark_importer,
        cloud139_importer=lambda: cloud139_importer,
        sixpan_importer=lambda: generic_importers.get("sixpan"),
        hide_full_links=lambda: _current_public_settings().get("hide_full_links", True),
        tianyi_importer=_active_tianyi_importer,
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
                cache_get=db.search_cache_queries.get_active,
                quark_importer=quark_importer,
                cloud139_importer=cloud139_importer,
                sixpan_importer=generic_importers.get("sixpan"),
                btbtla_client=btbtla,
                build_detail=_public_resource_detail,
                child_files=_public_resource_child_files,
                tianyi_importer=_active_tianyi_importer(),
            )
        )

    public_sixpan_preview_service = PublicSixpanPreviewService(
        cache_queries=db.search_cache_queries,
        importer=lambda: generic_importers.get("sixpan"),
        validate_url=lambda value: _validate_public_url(value, security_config),
        detect_link=detect_link,
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
        cache_queries=db.search_cache_queries,
        cache_commands=db.search_cache_commands,
        btbtla=lambda: btbtla,
        present_item=_public_resource_item,
        resource_detail=_public_resource_detail,
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
        source_type = str(getattr(link, "resource_kind", "") or "").strip().lower()
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
        search_cache=db.search_cache_queries.get_active,
        categories=lambda: app_config.categories,
        category=lambda key: app_config.category(key),
        limited_text=_limited_text,
        validate_url=_validate_public_url,
        safe_string_list=_safe_public_string_list,
        safe_quark_selection=_safe_public_quark_selection,
        safe_cloud139_selection=_safe_public_cloud139_selection,
        safe_tianyi_selection=_safe_public_tianyi_selection,
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
        get_request=db.guest_request_queries.get,
        add_event=db.guest_request_commands.add_event,
        update_request=db.guest_request_commands.update,
        merge_raw_data=_merge_raw_data,
        public_request=_public_request_response,
        auto_submit_allowed=_auto_submit_allowed,
    )

    def _guest_email_subscription_available() -> bool:
        try:
            notification_secrets.key_bytes()
        except ValueError:
            return False
        return notification_config.guest_email_available(
            notification_config.read_config(notification_repositories)
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
            notification_repositories.guest_notification_subscriptions.create(
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
                notification_repositories,
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
            tianyi_importer=_active_tianyi_importer(),
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
        payload = dict(request.get_json(silent=True) or {})
        selection_token = payload.pop("tmdb_selection_token", None)
        media_identity = public_tmdb_service.resolve_token(selection_token)
        payload.pop("media_identity", None)
        if media_identity:
            payload["media_identity"] = media_identity
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
                "request": _public_request_response(bound_request or db.guest_request_queries.get(guest_request_id)),
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
        subscription = db.guest_notification_subscriptions.verify(token)
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
        subscription = db.guest_notification_subscriptions.opt_out(token)
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
                "public": settings["public"],
                "submission": settings["submission"],
                "search": {
                    "providers": search_service.describe_providers(),
                    "result_provider_badges": _public_result_provider_badges(
                        app_config.raw.get("pansou")
                    ),
                },
                "category_labels": CATEGORY_LABELS,
            }
        )

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
        probe = get_optional_provider_method(importer, "probe")
        if not probe:
            return jsonify({"success": False, "message": "六盘离线适配器不可用"}), 400
        result = probe()
        return jsonify({"success": result.ok, "status": result.status, "message": result.message, "details": result.details})

    callback_service = RcloneCallbackService(
        CallbackDependencies(
            db=db,
            rclone=rclone_service,
            safe_int=_safe_int,
            callback_level=_rclone_callback_level,
            cancelled_status=JOB_CANCELLED,
            execution_repository=v1_transfer_dispatch.executions,
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
        if isinstance(error, ActiveProviderUnavailable):
            # An unactivated Provider account is a configuration state, not a
            # server fault: report it as 503 with the actionable message rather
            # than a 500 that hides which Provider needs activating.
            app.logger.warning("provider_unavailable error=%s", error)
            return jsonify(
                {
                    "success": False,
                    "error_code": "provider_unavailable",
                    "message": str(error),
                    "retryable": False,
                }
            ), 503
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
                readiness_status=readiness_service.status,
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
        accounts_blueprint_for_service(
            v1_accounts_service,
            admin_required,
            auth_sessions=provider_auth_sessions,
        )
    )
    app.register_blueprint(
        create_openlist_blueprint(
            OpenListRouteContext(
                admin_required=admin_required,
                handlers=default_handlers(
                    v1_openlist_connections,
                    v1_openlist_mounts,
                    connection_save=v1_storage_setup_service.save_openlist,
                ),
            )
        )
    )
    app.register_blueprint(
        create_storage_blueprint(
            StorageBlueprintContext(
                admin_required=admin_required,
                target_service=v1_target_service,
                assignment_service=v1_rules_service,
            )
        )
    )
    app.register_blueprint(
        create_storage_setup_blueprint(
            StorageSetupBlueprintContext(
                admin_required=admin_required,
                service=v1_storage_setup_service,
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
                },
            )
        )
    )
    if role_runs(active_process_role, "worker"):
        app.register_blueprint(
            create_rclone_worker_control_blueprint(
                RcloneWorkerControlContext(
                    token=lambda: os.getenv("RCLONE_WORKER_CONTROL_TOKEN", ""),
                    handlers=_rclone_worker_control_handlers(),
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
    })))
    app.register_blueprint(create_public_blueprint(PublicRouteContext(handlers={
        "index": index,
        "submit_page": submit_page,
        "request_status_page": request_status_page,
        "config": api_public_config,
        "trending": api_public_trending,
        "tmdb_search": api_public_tmdb_search,
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
    })))
    app.register_blueprint(create_sixpan_blueprint(SixPanRouteContext(admin_required=admin_required, handlers={
        "tasks": api_admin_sixpan_tasks,
        "probe": api_admin_sixpan_probe,
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
    app.register_blueprint(create_callbacks_blueprint(CallbackRouteContext(rclone_callback=api_rclone_callback)))
    if role_runs(active_process_role, "worker"):
        rclone_worker_runtime.start()
        # The queue is only a wake-up channel.  Reconcile the durable
        # transfer table before consuming new tasks so a crash between plan
        # persistence and enqueue (or a lost queue row) cannot strand work.
        try:
            recovery_result = transfer_recovery_scanner.scan_once()
            if recovery_result.errors:
                app.logger.warning(
                    "transfer recovery startup completed with errors: %s",
                    recovery_result.to_dict(),
                )
        except Exception:  # noqa: BLE001
            app.logger.exception("transfer recovery startup scan failed")
        transfer_recovery_runtime.start()
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
        if worker_watchdog is not None:
            worker_watchdog.start()
    return app
