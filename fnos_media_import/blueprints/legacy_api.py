from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from flask import Blueprint

@dataclass(frozen=True)
class LegacyApiRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]

def create_legacy_api_blueprint(context: LegacyApiRouteContext)->Blueprint:
    blueprint=Blueprint("legacy_api_routes",__name__)
    routes=[("/api/config/public","public_config","GET",False),("/api/search","search","POST",True),("/api/detect","detect","POST",True),("/api/import","import_resource","POST",True),("/api/jobs","jobs","GET",True)]
    for rule,endpoint,method,protected in routes:
        view=context.admin_required(context.handlers[endpoint]) if protected else context.handlers[endpoint]
        blueprint.add_url_rule(rule,endpoint=endpoint,view_func=view,methods=[method])
    return blueprint
