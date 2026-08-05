from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint


@dataclass(frozen=True)
class TrendingRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]


def create_trending_blueprint(context: TrendingRouteContext) -> Blueprint:
    blueprint = Blueprint("trending_routes", __name__)
    routes = [
        ("/api/admin/trending/status", "status", "GET"),
        ("/api/admin/trending/run", "run", "POST"),
        ("/api/admin/trending/runs", "runs", "GET"),
        ("/api/admin/trending/candidates", "candidates", "GET"),
        ("/api/admin/trending/candidates/<int:candidate_id>", "candidate_detail", "GET"),
        ("/api/admin/trending/candidates/<int:candidate_id>/search", "candidate_search", "POST"),
        ("/api/admin/trending/candidates/<int:candidate_id>/resources/<public_id>/detail", "candidate_resource_detail", "GET"),
        ("/api/admin/trending/candidates/<int:candidate_id>/resources/<public_id>/files", "candidate_resource_files", "GET"),
        ("/api/admin/trending/candidates/<int:candidate_id>/import", "candidate_import", "POST"),
        ("/api/admin/trending/candidates/<int:candidate_id>/subscribe", "candidate_subscribe", "POST"),
        ("/api/admin/trending/candidates/<int:candidate_id>/ignore", "candidate_ignore", "POST"),
        ("/api/admin/trending/candidates/<int:candidate_id>/restore", "candidate_restore", "POST"),
    ]
    for rule, endpoint, method in routes:
        blueprint.add_url_rule(
            rule,
            endpoint=endpoint,
            view_func=context.admin_required(context.handlers[endpoint]),
            methods=[method],
        )
    return blueprint
