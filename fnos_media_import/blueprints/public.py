from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from flask import Blueprint

@dataclass(frozen=True)
class PublicRouteContext:
    handlers: dict[str, Callable[..., Any]]

def create_public_blueprint(context: PublicRouteContext) -> Blueprint:
    blueprint = Blueprint("public_routes", __name__)
    routes = [
        ("/", "index", "GET"),
        ("/submit", "submit_page", "GET"),
        ("/request/<token>", "request_status_page", "GET"),
        ("/api/public/config", "config", "GET"),
        ("/api/public/trending", "trending", "GET"),
        ("/api/public/captcha", "captcha", "GET"),
        ("/api/public/search", "search", "POST"),
        ("/api/public/detect", "detect", "POST"),
        ("/api/public/manual/preview", "manual_preview", "POST"),
        ("/api/public/resources/<public_id>/detail", "resource_detail", "GET"),
        ("/api/public/resources/<public_id>/files", "resource_files", "GET"),
        ("/api/public/sixpan/parse", "sixpan_parse", "POST"),
        ("/api/public/btbtla/resolve", "btbtla_resolve", "POST"),
        ("/api/public/submit", "submit", "POST"),
        ("/api/public/request/<token>", "request", "GET"),
        ("/api/public/notifications/verify/<token>", "notification_verify_confirm", "GET"),
        ("/api/public/notifications/verify/<token>", "notification_verify", "POST"),
        ("/api/public/notifications/unsubscribe/<token>", "notification_unsubscribe_confirm", "GET"),
        ("/api/public/notifications/unsubscribe/<token>", "notification_unsubscribe", "POST"),
    ]
    for rule, endpoint, method in routes:
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=context.handlers[endpoint], methods=[method])
    return blueprint
