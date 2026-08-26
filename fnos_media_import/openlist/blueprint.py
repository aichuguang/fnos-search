"""OpenList 管理 API 路由（§16.2）。

``create_openlist_blueprint(context)`` 与仓库其它 blueprint 一致：只声明路由，
handler 由上下文提供。``default_handlers`` 提供可直接接入 app.py 的实现，全部
序列化均不包含密码明文或密文。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import secrets
from typing import Any, Callable

from flask import Blueprint, jsonify, request

from . import (
    OpenListCredentialError,
    OpenListError,
    OpenListMasterKeyError,
    OpenListNotFoundError,
    OpenListValidationError,
    OpenListWebDAVError,
)
from .connection_service import OpenListConnectionService
from .mount_service import OpenListMountService


logger = logging.getLogger("fnos_media_import.openlist_api")


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
        ("/api/admin/openlist/connections/<connection_id>", "delete_connection", "DELETE"),
        ("/api/admin/openlist/connections/<connection_id>/test", "test_connection", "POST"),
        ("/api/admin/openlist/mounts", "list_mounts", "GET"),
        ("/api/admin/openlist/mounts", "create_mount", "POST"),
        ("/api/admin/openlist/mounts/dirs", "mount_dirs", "GET"),
        ("/api/admin/openlist/mounts/<mount_id>", "update_mount", "PATCH"),
        ("/api/admin/openlist/mounts/<mount_id>/test", "test_mount", "POST"),
        ("/api/admin/openlist/mounts/<mount_id>/confirm-identity", "confirm_mount_identity", "POST"),
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
    *,
    connection_save: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Callable[..., Any]]:
    def json_payload(*, required: bool) -> dict[str, Any]:
        has_body = bool(request.get_data(cache=True))
        if not has_body and not required:
            return {}
        if not request.is_json:
            error = OpenListValidationError("请求体必须是 JSON 对象")
            error.code = "INVALID_JSON"
            error.status = 400
            raise error
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            error = OpenListValidationError("请求体必须是 JSON 对象")
            error.code = "INVALID_JSON"
            error.status = 400
            raise error
        return payload

    def list_connections() -> tuple[Any, int]:
        try:
            return jsonify({"success": True, "items": connection_service.list_connections()}), 200
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    def create_connection() -> tuple[Any, int]:
        try:
            payload = json_payload(required=True)
            if callable(connection_save):
                result = dict(connection_save(payload) or {})
                result.setdefault("success", True)
                return jsonify(result), 201
            item = connection_service.create_connection(payload)
        except Exception as exc:  # noqa: BLE001 - API boundary must always return JSON
            return _error_response(exc)
        return jsonify({"success": True, "item": item}), 201

    def update_connection(connection_id: str) -> tuple[Any, int]:
        try:
            payload = json_payload(required=True)
            if callable(connection_save):
                result = dict(
                    connection_save({**payload, "connection_id": connection_id}) or {}
                )
                result.setdefault("success", True)
                return jsonify(result), 200
            item = connection_service.update_connection(connection_id, payload)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return jsonify({"success": True, "item": item}), 200

    def test_connection(connection_id: str) -> tuple[Any, int]:
        try:
            payload = json_payload(required=False)
            result = connection_service.test_connection(
                connection_id,
                expected_revision=payload.get("expected_revision", payload.get("revision")),
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return jsonify(result), 200

    def delete_connection(connection_id: str) -> tuple[Any, int]:
        try:
            result = connection_service.delete_connection(connection_id)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return jsonify({"success": True, **result}), 200

    def list_mounts() -> tuple[Any, int]:
        try:
            connection_id = str(request.args.get("connection_id") or "").strip() or None
            return jsonify({"success": True, "items": mount_service.list_mounts(connection_id)}), 200
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    def create_mount() -> tuple[Any, int]:
        try:
            payload = json_payload(required=True)
            item = mount_service.create_mount(payload)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return jsonify({"success": True, "item": item}), 201

    def update_mount(mount_id: str) -> tuple[Any, int]:
        try:
            payload = json_payload(required=True)
            item = mount_service.update_mount(mount_id, payload)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return jsonify({"success": True, "item": item}), 200

    def test_mount(mount_id: str) -> tuple[Any, int]:
        try:
            payload = json_payload(required=False)
            result = mount_service.probe_mount(
                mount_id,
                expected_revision=payload.get("expected_revision", payload.get("revision")),
                expected_connection_revision=payload.get(
                    "expected_connection_revision", payload.get("connection_revision")
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return jsonify(result), 200

    def delete_mount(mount_id: str) -> tuple[Any, int]:
        try:
            result = mount_service.delete_mount(mount_id)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return jsonify({"success": True, **result}), 200

    def confirm_mount_identity(mount_id: str) -> tuple[Any, int]:
        try:
            payload = json_payload(required=True)
            if payload.get("expected_revision", payload.get("revision")) in (None, ""):
                error = OpenListValidationError("确认存储身份必须提供 expected_revision")
                error.code = "INVALID_REVISION"
                error.status = 400
                error.field_errors = {"expected_revision": "必填"}
                raise error
            if payload.get(
                "expected_connection_revision", payload.get("connection_revision")
            ) in (None, ""):
                error = OpenListValidationError(
                    "确认存储身份必须提供 expected_connection_revision"
                )
                error.code = "INVALID_REVISION"
                error.status = 400
                error.field_errors = {"expected_connection_revision": "必填"}
                raise error
            item = mount_service.confirm_identity(
                mount_id,
                expected_revision=payload.get("expected_revision", payload.get("revision")),
                expected_connection_revision=payload.get(
                    "expected_connection_revision", payload.get("connection_revision")
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return jsonify({"success": True, "item": item}), 200

    def mount_dirs() -> tuple[Any, int]:
        connection_id = str(request.args.get("connection_id") or "").strip()
        # path 留空即列 WebDAV 根，前端目录树按此逐级展开。
        path = str(request.args.get("path") or "").strip()
        try:
            items = mount_service.list_mountable_dirs(connection_id, path)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return jsonify({"success": True, "items": items}), 200

    return {
        "list_connections": list_connections,
        "create_connection": create_connection,
        "update_connection": update_connection,
        "test_connection": test_connection,
        "delete_connection": delete_connection,
        "list_mounts": list_mounts,
        "create_mount": create_mount,
        "update_mount": update_mount,
        "test_mount": test_mount,
        "delete_mount": delete_mount,
        "confirm_mount_identity": confirm_mount_identity,
        "mount_dirs": mount_dirs,
    }


def _trace_id() -> str:
    supplied = str(request.headers.get("X-Request-ID") or "").strip()
    return (supplied or f"openlist_{secrets.token_hex(8)}")[:80]


def _error_response(exc: BaseException) -> tuple[Any, int]:
    trace_id = _trace_id()
    explicit_status = getattr(exc, "status", None)
    if explicit_status is not None:
        status = int(explicit_status)
    elif isinstance(exc, OpenListNotFoundError):
        status = 404
    elif isinstance(exc, (OpenListCredentialError, OpenListWebDAVError)):
        status = 503
    elif isinstance(exc, OpenListMasterKeyError):
        status = 503
    elif isinstance(exc, OpenListValidationError):
        status = 422
    elif isinstance(exc, OpenListError):
        status = 409
    else:
        status = 500
    code = str(getattr(exc, "code", "") or type(exc).__name__).upper()
    retryable = bool(getattr(exc, "retryable", False)) or isinstance(
        exc, (OpenListCredentialError, OpenListWebDAVError)
    )
    field_errors = dict(getattr(exc, "field_errors", {}) or {})
    if status >= 500:
        logger.exception(
            "event=openlist_api_failed trace_id=%s method=%s path=%s code=%s status=%s",
            trace_id,
            request.method,
            request.path,
            code,
            status,
        )
    else:
        logger.warning(
            "event=openlist_api_failed trace_id=%s method=%s path=%s code=%s status=%s message=%s",
            trace_id,
            request.method,
            request.path,
            code,
            status,
            str(exc)[:500],
        )
    public_error = isinstance(exc, OpenListError) or explicit_status is not None
    payload: dict[str, Any] = {
        "success": False,
        "code": code if status < 500 or public_error else "INTERNAL_ERROR",
        "message": str(exc) if public_error else "OpenList 管理服务发生内部错误",
        "trace_id": trace_id,
        "retryable": retryable,
    }
    if field_errors:
        payload["field_errors"] = field_errors
    return jsonify(payload), status


__all__ = ["OpenListRouteContext", "create_openlist_blueprint", "default_handlers"]
