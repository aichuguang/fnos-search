from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint


@dataclass(frozen=True)
class RcloneRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]


def create_rclone_blueprint(context: RcloneRouteContext) -> Blueprint:
    blueprint = Blueprint("rclone_routes", __name__)
    routes = [
        ("/api/admin/rclone/status", "admin_status", "GET"),
        ("/api/admin/rclone/start", "admin_start", "POST"),
        ("/api/admin/rclone/stop", "admin_stop", "POST"),
        ("/api/admin/rclone/logs", "admin_logs", "GET"),
        ("/api/admin/rclone/runs", "admin_runs", "GET"),
        ("/api/admin/rclone/events", "admin_events", "GET"),
        ("/api/admin/rclone/files", "admin_files", "GET"),
        ("/api/admin/rclone/files/<int:event_id>/retry", "admin_file_retry", "POST"),
        ("/api/admin/rclone/check", "admin_check", "GET"),
        ("/api/admin/rclone/webdav-config", "admin_webdav_config", "GET"),
        ("/api/admin/rclone/webdav-config", "admin_webdav_config_update", "POST"),
        ("/api/admin/rclone/webdav-config/test", "admin_webdav_config_test", "POST"),
        ("/api/rclone/status", "legacy_status", "GET"),
        ("/api/rclone/start", "legacy_start", "POST"),
        ("/api/rclone/stop", "legacy_stop", "POST"),
        ("/api/rclone/logs", "legacy_logs", "GET"),
        ("/api/rclone/runs", "legacy_runs", "GET"),
        ("/api/rclone/events", "legacy_events", "GET"),
        ("/api/rclone/files", "legacy_files", "GET"),
        ("/api/rclone/check", "legacy_check", "GET"),
    ]
    for rule, endpoint, method in routes:
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=context.admin_required(context.handlers[endpoint]), methods=[method])
    return blueprint
