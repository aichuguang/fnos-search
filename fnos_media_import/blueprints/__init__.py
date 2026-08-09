"""Flask blueprint factories for low-coupling application routes."""

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
from .sixpan import SixPanRouteContext, create_sixpan_blueprint
from .adapters import AdaptersRouteContext, create_adapters_blueprint
from .admin_shell import AdminShellRouteContext, create_admin_shell_blueprint
from .diagnostics import DiagnosticsRouteContext, create_diagnostics_blueprint
from .callbacks import CallbackRouteContext, create_callbacks_blueprint
from .trending import TrendingRouteContext, create_trending_blueprint
from .rclone_worker_control import RcloneWorkerControlContext, create_rclone_worker_control_blueprint


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
    "SixPanRouteContext",
    "AdaptersRouteContext",
    "AdminShellRouteContext",
    "DiagnosticsRouteContext",
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
    "create_sixpan_blueprint",
    "create_adapters_blueprint",
    "create_admin_shell_blueprint",
    "create_diagnostics_blueprint",
    "create_callbacks_blueprint",
    "create_trending_blueprint",
    "create_rclone_worker_control_blueprint",
]
