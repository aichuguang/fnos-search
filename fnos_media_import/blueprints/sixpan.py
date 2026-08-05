from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from flask import Blueprint

@dataclass(frozen=True)
class SixPanRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]

def create_sixpan_blueprint(context: SixPanRouteContext) -> Blueprint:
    blueprint = Blueprint("sixpan_routes", __name__)
    routes = [
        ("/api/admin/sixpan/tasks", "tasks", "GET"),
        ("/api/admin/sixpan/probe", "probe", "POST"),
        ("/api/admin/sixpan/oauth/device-code", "oauth_device_code", "POST"),
        ("/api/admin/sixpan/oauth/device-code/check", "oauth_device_code_check", "POST"),
        ("/api/admin/sixpan/sync", "sync", "POST"),
        (
            "/api/admin/sixpan/jobs/<int:job_id>/retry-media-refresh",
            "retry_media_refresh",
            "POST",
        ),
    ]
    for rule, endpoint, method in routes:
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=context.admin_required(context.handlers[endpoint]), methods=[method])
    return blueprint
