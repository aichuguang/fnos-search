from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from flask import Blueprint

@dataclass(frozen=True)
class AdminShellRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]

def create_admin_shell_blueprint(context: AdminShellRouteContext) -> Blueprint:
    blueprint=Blueprint("admin_shell_routes",__name__)
    public=[("/admin/login","login_page"),("/admin","admin_page"),("/admin/requests","admin_page"),("/admin/jobs","admin_page")]
    for rule,endpoint in public: blueprint.add_url_rule(rule,endpoint=endpoint,view_func=context.handlers[endpoint],methods=["GET"])
    protected=[
        ("/api/admin/profile","profile","GET"),("/api/admin/profile","profile_update","POST"),("/api/admin/profile","profile_update","PUT"),
        ("/api/admin/profile/avatar","avatar","POST"),("/api/admin/site-logo","site_logo","POST"),
    ]
    views={endpoint:context.admin_required(context.handlers[endpoint]) for _,endpoint,_ in protected}
    for rule,endpoint,method in protected: blueprint.add_url_rule(rule,endpoint=endpoint,view_func=views[endpoint],methods=[method])
    return blueprint
