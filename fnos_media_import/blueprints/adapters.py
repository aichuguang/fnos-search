from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from flask import Blueprint

@dataclass(frozen=True)
class AdaptersRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]

def create_adapters_blueprint(context: AdaptersRouteContext) -> Blueprint:
    blueprint = Blueprint("adapter_routes", __name__)
    routes = [
        ("/api/admin/search/providers", "search_providers", "GET"),
        ("/api/admin/search/providers", "search_providers_update", "POST"),
        ("/api/admin/search/providers", "search_providers_update", "PUT"),
        ("/api/admin/search/aliases", "search_aliases", "GET"),
        ("/api/admin/search/aliases", "search_aliases_update", "POST"),
        ("/api/admin/search/aliases", "search_aliases_update", "PUT"),
        ("/api/admin/adapters", "adapters", "GET"),
        ("/api/admin/adapters/<adapter_key>/probe", "adapter_probe", "POST"),
    ]
    protected = {endpoint: context.admin_required(context.handlers[endpoint]) for _, endpoint, _ in routes}
    for rule, endpoint, method in routes:
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=protected[endpoint], methods=[method])
    return blueprint
