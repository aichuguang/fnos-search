"""OpenList 管理 API 路由（§16.2）。

``create_openlist_blueprint(context)`` 与仓库其它 blueprint 一致：只声明路由，
handler 由上下文提供。``default_handlers`` 提供可直接接入 app.py 的实现，全部
序列化均不包含密码明文或密文。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, jsonify, request

from . import OpenListError, OpenListMasterKeyError, OpenListNotFoundError, OpenListValidationError
from .connection_service import OpenListConnectionService
from .mount_service import OpenListMountService


@dataclass(frozen=True)
class OpenListRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    handlers: dict[str, Callable[..., Any]]


def create_openlist_blueprint(context: OpenListRouteContext) -> Blueprint:
    blueprint = Blueprint("openlist", __name__)
    routes = [
        ("/api/admin/openlist/connections", "list_connections", "GET"),
        ("/api/admin/openlist/connections", "create_connection", "POST"),
        ("/api/admin/openlist/connections/<connection_id>", "update_connection", "PATCH"),
        ("/api/admin/openlist/connections/<connection_id>/test", "test_connection", "POST"),
        ("/api/admin/openlist/mounts", "list_mounts", "GET"),
        ("/api/admin/openlist/mounts", "create_mount", "POST"),
        ("/api/admin/openlist/mounts/dirs", "mount_dirs", "GET"),
        ("/api/admin/openlist/mounts/<mount_id>", "update_mount", "PATCH"),
        ("/api/admin/openlist/mounts/<mount_id>/test", "test_mount", "POST"),
        ("/api/admin/openlist/mounts/<mount_id>", "delete_mount", "DELETE"),
    ]
    for rule, endpoint, method in routes:
        blueprint.add_url_rule(
            rule,
            endpoint=endpoint,
            view_func=context.admin_required(context.handlers[endpoint]),
            methods=[method],
        )
    return blueprint


def default_handlers(
    connection_service: OpenListConnectionService,
    mount_service: OpenListMountService,
) -> dict[str, Callable[..., Any]]:
    def list_connections() -> tuple[Any, int]:
        return jsonify({"success": True, "items": connection_service.list_connections()}), 200

    def create_connection() -> tuple[Any, int]:
        payload = request.get_json(silent=True) or {}
        try:
            item = connection_service.create_connection(payload)
        except OpenListError as exc:
            return _error_response(exc)
        return jsonify({"success": True, "item": item}), 201

    def update_connection(connection_id: str) -> tuple[Any, int]:
        payload = request.get_json(silent=True) or {}
        try:
            item = connection_service.update_connection(connection_id, payload)
        except OpenListError as exc:
            return _error_response(exc)
        return jsonify({"success": True, "item": item}), 200

    def test_connection(connection_id: str) -> tuple[Any, int]:
        try:
            result = connection_service.test_connection(connection_id)
        except OpenListError as exc:
            return _error_response(exc)
        return jsonify(result), 200

    def list_mounts() -> tuple[Any, int]:
        connection_id = str(request.args.get("connection_id") or "").strip() or None
        return jsonify({"success": True, "items": mount_service.list_mounts(connection_id)}), 200

    def create_mount() -> tuple[Any, int]:
        payload = request.get_json(silent=True) or {}
        try:
            item = mount_service.create_mount(payload)
        except OpenListError as exc:
            return _error_response(exc)
        return jsonify({"success": True, "item": item}), 201

    def update_mount(mount_id: str) -> tuple[Any, int]:
        payload = request.get_json(silent=True) or {}
        try:
            item = mount_service.update_mount(mount_id, payload)
        except OpenListError as exc:
            return _error_response(exc)
        return jsonify({"success": True, "item": item}), 200

    def test_mount(mount_id: str) -> tuple[Any, int]:
        try:
            result = mount_service.probe_mount(mount_id)
        except OpenListError as exc:
            return _error_response(exc)
        return jsonify(result), 200

    def delete_mount(mount_id: str) -> tuple[Any, int]:
        try:
            result = mount_service.delete_mount(mount_id)
        except OpenListError as exc:
            return _error_response(exc)
        return jsonify({"success": True, **result}), 200

    def mount_dirs() -> tuple[Any, int]:
        connection_id = str(request.args.get("connection_id") or "").strip()
        try:
            items = mount_service.list_mountable_dirs(connection_id)
        except OpenListError as exc:
            return _error_response(exc)
        return jsonify({"success": True, "items": items}), 200

    return {
        "list_connections": list_connections,
        "create_connection": create_connection,
        "update_connection": update_connection,
        "test_connection": test_connection,
        "list_mounts": list_mounts,
        "create_mount": create_mount,
        "update_mount": update_mount,
        "test_mount": test_mount,
        "delete_mount": delete_mount,
        "mount_dirs": mount_dirs,
    }


def _error_response(exc: OpenListError) -> tuple[Any, int]:
    if isinstance(exc, OpenListNotFoundError):
        status = 404
    elif isinstance(exc, (OpenListValidationError, OpenListMasterKeyError)):
        status = 400
    else:
        status = 400
    return jsonify({"success": False, "code": type(exc).__name__, "message": str(exc)}), status


__all__ = ["OpenListRouteContext", "create_openlist_blueprint", "default_handlers"]
