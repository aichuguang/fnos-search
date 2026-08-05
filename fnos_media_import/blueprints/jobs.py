from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from flask import Blueprint

@dataclass(frozen=True)
class JobsRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]

def create_jobs_blueprint(context: JobsRouteContext) -> Blueprint:
    blueprint = Blueprint("job_routes", __name__)
    routes = [
        ("/api/admin/jobs", "admin_jobs", "GET"),
        ("/api/admin/jobs/<int:job_id>", "admin_job_detail", "GET"),
        ("/api/admin/jobs/<int:job_id>/retry", "admin_job_retry", "POST"),
        ("/api/admin/jobs/<int:job_id>/cancel", "admin_job_cancel", "POST"),
        ("/api/admin/jobs/<int:job_id>", "admin_job_delete", "DELETE"),
        ("/api/admin/jobs/batch-retry", "admin_jobs_batch_retry", "POST"),
        ("/api/jobs/<int:job_id>", "legacy_job_detail", "GET"),
        ("/api/jobs/<int:job_id>/retry", "legacy_job_retry", "POST"),
    ]
    for rule, endpoint, method in routes:
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=context.admin_required(context.handlers[endpoint]), methods=[method])
    return blueprint
