"""Flask blueprint factories for low-coupling application routes."""

from flask import Flask

from .auth import AuthRouteContext, create_auth_blueprint
from .system import SystemRouteContext, create_system_blueprint
from .settings import SettingsRouteContext, create_settings_blueprint
from .rclone import RcloneRouteContext, create_rclone_blueprint
from .organizer import OrganizerRouteContext, create_organizer_blueprint
from .updates import UpdatesRouteContext, create_updates_blueprint
from .requests import RequestsRouteContext, create_requests_blueprint
from .jobs import JobsRouteContext, create_jobs_blueprint
from .public import PublicRouteContext, create_public_blueprint
from .media import MediaRouteContext, create_media_blueprint
from .cloud_compat import CloudCompatRouteContext, create_cloud_compat_blueprint
from .sixpan import SixPanRouteContext, create_sixpan_blueprint
from .adapters import AdaptersRouteContext, create_adapters_blueprint
from .admin_shell import AdminShellRouteContext, create_admin_shell_blueprint
from .diagnostics import DiagnosticsRouteContext, create_diagnostics_blueprint
from .legacy_api import LegacyApiRouteContext, create_legacy_api_blueprint
from .callbacks import CallbackRouteContext, create_callbacks_blueprint
from .trending import TrendingRouteContext, create_trending_blueprint
from .rclone_worker_control import RcloneWorkerControlContext, create_rclone_worker_control_blueprint


def preserve_legacy_endpoints(app: Flask, mapping: dict[str, str]) -> None:
    """Remove Blueprint prefixes while retaining the original endpoint contract."""
    for rule in app.url_map.iter_rules():
        legacy_endpoint = mapping.get(rule.endpoint)
        if legacy_endpoint:
            rule.endpoint = legacy_endpoint
    for blueprint_endpoint, legacy_endpoint in mapping.items():
        view = app.view_functions.pop(blueprint_endpoint, None)
        if view is not None:
            app.view_functions[legacy_endpoint] = view
    # Werkzeug indexes rules by endpoint during registration. Because the
    # compatibility rename happens immediately afterwards, rebuild that index
    # before the application starts serving requests.
    rules_by_endpoint: dict[str, list[object]] = {}
    for rule in app.url_map.iter_rules():
        rules_by_endpoint.setdefault(rule.endpoint, []).append(rule)
    app.url_map._rules_by_endpoint = rules_by_endpoint


__all__ = [
    "AuthRouteContext",
    "SystemRouteContext",
    "SettingsRouteContext",
    "RcloneRouteContext",
    "OrganizerRouteContext",
    "UpdatesRouteContext",
    "RequestsRouteContext",
    "JobsRouteContext",
    "PublicRouteContext",
    "MediaRouteContext",
    "CloudCompatRouteContext",
    "SixPanRouteContext",
    "AdaptersRouteContext",
    "AdminShellRouteContext",
    "DiagnosticsRouteContext",
    "LegacyApiRouteContext",
    "CallbackRouteContext",
    "TrendingRouteContext",
    "RcloneWorkerControlContext",
    "create_auth_blueprint",
    "create_system_blueprint",
    "create_settings_blueprint",
    "create_rclone_blueprint",
    "create_organizer_blueprint",
    "create_updates_blueprint",
    "create_requests_blueprint",
    "create_jobs_blueprint",
    "create_public_blueprint",
    "create_media_blueprint",
    "create_cloud_compat_blueprint",
    "create_sixpan_blueprint",
    "create_adapters_blueprint",
    "create_admin_shell_blueprint",
    "create_diagnostics_blueprint",
    "create_legacy_api_blueprint",
    "create_callbacks_blueprint",
    "create_trending_blueprint",
    "create_rclone_worker_control_blueprint",
    "preserve_legacy_endpoints",
]
