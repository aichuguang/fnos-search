from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from flask import Blueprint

@dataclass(frozen=True)
class CloudCompatRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]

def create_cloud_compat_blueprint(context: CloudCompatRouteContext) -> Blueprint:
    blueprint = Blueprint("cloud_compat_routes", __name__)
    routes = [
        ("/api/quark/check", "quark_check"),
        ("/api/quark/file-list", "quark_file_list"),
        ("/api/cloud139/check", "cloud139_check"),
        ("/api/cloud139/file-list", "cloud139_file_list"),
    ]
    for rule, endpoint in routes:
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=context.admin_required(context.handlers[endpoint]), methods=["POST"])
    return blueprint
