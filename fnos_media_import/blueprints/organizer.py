from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint


@dataclass(frozen=True)
class OrganizerRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]


def create_organizer_blueprint(context: OrganizerRouteContext) -> Blueprint:
    blueprint = Blueprint("organizer_routes", __name__)
    routes = [
        ("/api/admin/organizer/tasks", "tasks", "GET"),
        ("/api/admin/organizer/tasks/scan", "scan", "POST"),
        ("/api/admin/organizer/tasks/<int:task_id>", "task_detail", "GET"),
        ("/api/admin/organizer/tasks/<int:task_id>", "delete", "DELETE"),
        ("/api/admin/organizer/tasks/<int:task_id>/rebuild", "rebuild", "POST"),
        ("/api/admin/organizer/tasks/<int:task_id>/mappings/<int:mapping_id>", "mapping_update", "PATCH"),
        ("/api/admin/organizer/tasks/<int:task_id>/mappings/batch", "mappings_batch_update", "POST"),
        ("/api/admin/organizer/tasks/<int:task_id>/approve", "approve", "POST"),
        ("/api/admin/organizer/tasks/<int:task_id>/apply", "apply", "POST"),
        ("/api/admin/organizer/tasks/<int:task_id>/skip", "skip", "POST"),
        ("/api/admin/organizer/tasks/<int:task_id>/retry", "retry", "POST"),
        ("/api/admin/organizer/runs", "runs", "GET"),
        ("/api/admin/organizer/runs/<int:run_id>/rollback", "rollback", "POST"),
    ]
    for rule, endpoint, method in routes:
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=context.admin_required(context.handlers[endpoint]), methods=[method])
    return blueprint
