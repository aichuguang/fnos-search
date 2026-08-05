from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from flask import Blueprint

@dataclass(frozen=True)
class RequestsRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]

def create_requests_blueprint(context: RequestsRouteContext) -> Blueprint:
    blueprint = Blueprint("request_routes", __name__)
    routes = [
        ("/api/admin/dashboard", "dashboard", "GET"),
        ("/api/admin/requests", "requests", "GET"),
        ("/api/admin/requests/<int:request_id>", "request_detail", "GET"),
        ("/api/admin/requests/<int:request_id>/approve", "request_approve", "POST"),
        ("/api/admin/requests/<int:request_id>/reject", "request_reject", "POST"),
        ("/api/admin/requests/<int:request_id>/cancel", "request_cancel", "POST"),
    ]
    for rule, endpoint, method in routes:
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=context.admin_required(context.handlers[endpoint]), methods=[method])
    return blueprint
