from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint


@dataclass(frozen=True)
class UpdatesRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]


def create_updates_blueprint(context: UpdatesRouteContext) -> Blueprint:
    blueprint = Blueprint("update_routes", __name__)
    routes = [
        ("/api/admin/update-subscriptions", "subscriptions", "GET"),
        ("/api/admin/update-subscriptions", "subscription_create", "POST"),
        ("/api/admin/update-subscriptions/<int:subscription_id>", "subscription_detail", "GET"),
        ("/api/admin/update-subscriptions/<int:subscription_id>", "subscription_update", "PUT"),
        ("/api/admin/update-subscriptions/<int:subscription_id>", "subscription_delete", "DELETE"),
        ("/api/admin/update-subscriptions/<int:subscription_id>/run", "subscription_run", "POST"),
        ("/api/admin/update-subscriptions/<int:subscription_id>/refresh-snapshot", "subscription_refresh_snapshot", "POST"),
        ("/api/admin/update-subscriptions/<int:subscription_id>/preview", "subscription_preview", "POST"),
        ("/api/admin/update-subscriptions/<int:subscription_id>/pause", "subscription_pause", "POST"),
        ("/api/admin/update-subscriptions/<int:subscription_id>/enable", "subscription_enable", "POST"),
        ("/api/admin/update-runs", "runs", "GET"),
        ("/api/admin/update-runs/<int:run_id>", "run_detail", "GET"),
        ("/api/admin/update-candidates", "candidates", "GET"),
        ("/api/admin/update-candidates/<int:candidate_id>/import", "candidate_import", "POST"),
        ("/api/admin/update-candidates/<int:candidate_id>/reject", "candidate_reject", "POST"),
        ("/api/admin/update-scheduler/run-due", "scheduler_run_due", "POST"),
        ("/api/admin/update-scheduler/status", "scheduler_status", "GET"),
    ]
    for rule, endpoint, method in routes:
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=context.admin_required(context.handlers[endpoint]), methods=[method])
    return blueprint
