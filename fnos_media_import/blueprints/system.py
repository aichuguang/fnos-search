from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, jsonify, render_template

from ..openapi import get_openapi_spec


@dataclass(frozen=True)
class SystemRouteContext:
    app_name: Callable[[], str]
    readiness_status: Callable[[], dict[str, Any]]
    dependency_status: Callable[[], dict[str, Any]]
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    log_readiness_error: Callable[[BaseException], None]


def create_system_blueprint(context: SystemRouteContext) -> Blueprint:
    blueprint = Blueprint("system", __name__)

    @blueprint.get("/health")
    def health():
        return jsonify({"ok": True, "name": context.app_name()})

    @blueprint.get("/livez")
    def livez():
        return jsonify({"ok": True, "status": "alive", "name": context.app_name()})

    @blueprint.get("/readyz")
    def readyz():
        try:
            status = context.readiness_status()
            return jsonify(status), 200 if status.get("ok") else 503
        except Exception as exc:  # noqa: BLE001
            context.log_readiness_error(exc)
            return jsonify({"ok": False, "status": "not_ready", "database": "error", "message": "就绪检查执行失败"}), 503

    @blueprint.get("/dependencies")
    @context.admin_required
    def dependencies():
        return jsonify(context.dependency_status())

    @blueprint.get("/openapi.json")
    def openapi_json():
        return jsonify(get_openapi_spec())

    @blueprint.get("/swagger")
    @blueprint.get("/api-docs")
    def swagger_docs():
        return render_template("swagger.html", app_name=context.app_name())

    return blueprint
