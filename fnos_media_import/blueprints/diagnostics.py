from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from flask import Blueprint

@dataclass(frozen=True)
class DiagnosticsRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]

def create_diagnostics_blueprint(context: DiagnosticsRouteContext)->Blueprint:
    blueprint=Blueprint("diagnostic_routes",__name__)
    routes=[("/api/admin/system/logs","system_logs","GET"),("/api/admin/system/events","system_events","GET"),("/api/admin/system/task-logs","task_logs","GET"),("/api/admin/btbtla/proxy-test","btbtla_proxy_test","POST"),("/api/admin/openlist/test","openlist_test","POST"),("/api/admin/openlist/dirs","openlist_dirs","GET"),("/api/admin/tmdb/test","tmdb_test","POST"),("/api/admin/tmdb/search","tmdb_search","GET"),("/api/admin/tmdb/<media_type>/<int:tmdb_id>","tmdb_detail","GET"),("/api/admin/ai/test","ai_test","POST")]
    for rule,endpoint,method in routes: blueprint.add_url_rule(rule,endpoint=endpoint,view_func=context.admin_required(context.handlers[endpoint]),methods=[method])
    return blueprint
