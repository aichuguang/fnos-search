from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, jsonify, request


@dataclass(frozen=True)
class RcloneWorkerControlContext:
    token: Callable[[], str]
    handlers: dict[str, Callable[..., Any]]


def create_rclone_worker_control_blueprint(context: RcloneWorkerControlContext) -> Blueprint:
    blueprint = Blueprint("rclone_worker_control", __name__)

    def authorized() -> bool:
        expected = str(context.token() or "").strip()
        supplied = str(request.headers.get("Authorization") or "").strip()
        if not supplied.lower().startswith("bearer "):
            return False
        supplied = supplied[7:].strip()
        return bool(expected and supplied and hmac.compare_digest(expected, supplied))

    def dispatch(name: str):
        if not authorized():
            return jsonify({"success": False, "message": "rclone Worker 控制认证失败"}), 401
        return context.handlers[name]()

    blueprint.add_url_rule(
        "/api/internal/rclone/status",
        endpoint="status",
        view_func=lambda: dispatch("status"),
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/internal/rclone/logs",
        endpoint="logs",
        view_func=lambda: dispatch("logs"),
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/internal/worker/status",
        endpoint="worker_status",
        view_func=lambda: dispatch("worker_status"),
        methods=["GET"],
    )
    for endpoint in (
        "start",
        "stop",
        "check",
        "cancel_job",
        "cleanup_cancelled_task",
        "file_retry",
        "runtime_reload",
        "webdav_config_update",
        "webdav_config_test",
    ):
        rule = {
            "start": "/api/internal/rclone/start",
            "stop": "/api/internal/rclone/stop",
            "check": "/api/internal/rclone/check",
            "cancel_job": "/api/internal/rclone/cancel-job",
            "cleanup_cancelled_task": "/api/internal/rclone/cleanup-cancelled-task",
            "file_retry": "/api/internal/rclone/file-retry",
            "runtime_reload": "/api/internal/runtime/reload",
            "webdav_config_update": "/api/internal/rclone/webdav-config",
            "webdav_config_test": "/api/internal/rclone/webdav-config/test",
        }[endpoint]
        blueprint.add_url_rule(
            rule,
            endpoint=endpoint,
            view_func=lambda name=endpoint: dispatch(name),
            methods=["POST"],
        )
    blueprint.add_url_rule(
        "/api/internal/rclone/webdav-config",
        endpoint="webdav_config",
        view_func=lambda: dispatch("webdav_config"),
        methods=["GET"],
    )
    return blueprint
