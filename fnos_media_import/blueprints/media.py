from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from flask import Blueprint

@dataclass(frozen=True)
class MediaRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]

def create_media_blueprint(context: MediaRouteContext) -> Blueprint:
    blueprint = Blueprint("media_routes", __name__)
    routes = [
        ("/api/admin/media/libraries", "libraries", "GET"),
        ("/api/admin/media/running", "running", "GET"),
        ("/api/admin/media/refresh-logs", "refresh_logs", "GET"),
        ("/api/admin/media/refresh", "admin_refresh", "POST"),
        ("/api/media/refresh", "legacy_refresh", "POST"),
    ]
    for rule, endpoint, method in routes:
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=context.admin_required(context.handlers[endpoint]), methods=[method])
    return blueprint
