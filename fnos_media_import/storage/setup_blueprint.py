"""多链路管理后台聚合 API。

该 Blueprint 只暴露产品级对象（账号、OpenList 连接、保存位置和来源规则），
并统一返回可排障且不泄露凭证的错误信封。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import secrets
from typing import Any, Callable

from flask import Blueprint, jsonify, request

from ..accounts.crypto import MasterKeyError
from ..accounts.repository import ProviderAccountError
from ..openlist import (
    OpenListCredentialError,
    OpenListError,
    OpenListMasterKeyError,
    OpenListNotFoundError,
    OpenListValidationError,
    OpenListWebDAVError,
)
from .assignment_service import AssignmentConflictError, AssignmentError
from .setup_service import StorageSetupError, StorageSetupService
from .target_service import (
    TargetError,
    TargetConflictError,
    TargetInUseError,
    TargetNotFoundError,
    TargetRevisionNotFoundError,
    TargetValidationError,
)


logger = logging.getLogger("fnos_media_import.storage_setup")


@dataclass(frozen=True)
class StorageSetupBlueprintContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    service: StorageSetupService


def _trace_id() -> str:
    supplied = str(request.headers.get("X-Request-ID") or "").strip()
    return (supplied or f"storage_{secrets.token_hex(8)}")[:80]


def _error_meta(exc: BaseException) -> tuple[str, int, bool, dict[str, str]]:
    code = str(getattr(exc, "code", "") or type(exc).__name__).upper()
    explicit_status = getattr(exc, "status", None)
    retryable = bool(getattr(exc, "retryable", False))
    field_errors = dict(getattr(exc, "field_errors", {}) or {})
    if explicit_status is not None:
        return code, int(explicit_status), retryable, field_errors
    if isinstance(exc, (TargetNotFoundError, TargetRevisionNotFoundError, OpenListNotFoundError)):
        return code, 404, False, field_errors
    if isinstance(exc, TargetInUseError):
        return code, 409, False, field_errors
    if isinstance(exc, TargetConflictError):
        return "TARGET_CONFLICT", 409, False, field_errors
    if isinstance(exc, AssignmentConflictError):
        return "ASSIGNMENT_CONFLICT", 409, False, field_errors
    if isinstance(exc, ProviderAccountError):
        account_status = {
            "ACCOUNT_NOT_FOUND": 404,
            "ACCOUNT_CONFLICT": 409,
            "ACCOUNT_ACTIVE": 409,
            "ACCOUNT_IN_USE": 409,
            "SELECTION_CHANGED": 409,
            "ACCOUNT_DISABLED": 409,
            "ACCOUNT_NOT_TESTED": 409,
            "INVALID_CONFIG": 422,
            "IMMUTABLE_FIELD": 409,
        }
        return code, account_status.get(code, 422), False, field_errors
    if isinstance(exc, (OpenListWebDAVError, OpenListCredentialError)):
        return code, 503, True, field_errors
    if isinstance(exc, (MasterKeyError, OpenListMasterKeyError)):
        return "MASTER_KEY_UNAVAILABLE", 503, False, field_errors
    if isinstance(exc, (StorageSetupError, OpenListValidationError, AssignmentError, TargetValidationError)):
        return code, 422, retryable, field_errors
    if isinstance(exc, (OpenListError, TargetError)):
        return code, 409, retryable, field_errors
    return code or "INTERNAL_ERROR", 500, False, field_errors


def create_storage_setup_blueprint(context: StorageSetupBlueprintContext) -> Blueprint:
    blueprint = Blueprint("storage_setup", __name__)
    protected = context.admin_required
    service = context.service

    def guarded(callback: Callable[[], Any]) -> Any:
        trace_id = _trace_id()
        try:
            return callback()
        except (
            StorageSetupError,
            ProviderAccountError,
            OpenListError,
            AssignmentError,
            TargetError,
            MasterKeyError,
        ) as exc:
            code, status, retryable, field_errors = _error_meta(exc)
            logger.warning(
                "event=storage_setup_failed trace_id=%s method=%s path=%s code=%s status=%s retryable=%s message=%s",
                trace_id,
                request.method,
                request.path,
                code,
                status,
                retryable,
                str(exc)[:500],
            )
            payload: dict[str, Any] = {
                "success": False,
                "code": code,
                "message": str(exc),
                "trace_id": trace_id,
                "retryable": retryable,
            }
            if field_errors:
                payload["field_errors"] = field_errors
            return jsonify(payload), status
        except Exception:  # noqa: BLE001 - API boundary must not return HTML errors
            logger.exception(
                "event=storage_setup_failed trace_id=%s method=%s path=%s code=INTERNAL_ERROR",
                trace_id,
                request.method,
                request.path,
            )
            return jsonify(
                {
                    "success": False,
                    "code": "INTERNAL_ERROR",
                    "message": "多链路配置服务发生内部错误",
                    "trace_id": trace_id,
                    "retryable": False,
                }
            ), 500

    def json_payload() -> dict[str, Any]:
        if not request.is_json:
            raise StorageSetupError(
                "请求体必须是 JSON 对象",
                code="INVALID_JSON",
                status=400,
            )
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise StorageSetupError(
                "请求体必须是 JSON 对象",
                code="INVALID_JSON",
                status=400,
            )
        return payload

    @protected
    def snapshot() -> Any:
        return guarded(lambda: jsonify(service.snapshot()))

    @protected
    def save_account() -> Any:
        return guarded(lambda: jsonify(service.save_account(json_payload())))

    @protected
    def save_openlist() -> Any:
        return guarded(lambda: jsonify(service.save_openlist(json_payload())))

    @protected
    def delete_openlist(connection_id: str) -> Any:
        return guarded(
            lambda: jsonify(
                {
                    "success": True,
                    **service.delete_openlist(connection_id),
                    "message": "OpenList 连接已删除",
                }
            )
        )

    @protected
    def create_location() -> Any:
        return guarded(lambda: (jsonify(service.save_location(json_payload())), 201))

    @protected
    def update_location(destination_id: str) -> Any:
        return guarded(
            lambda: jsonify(service.save_location(json_payload(), destination_id))
        )

    @protected
    def delete_location(destination_id: str) -> Any:
        def run() -> Any:
            result = service.delete_location(destination_id)
            return jsonify({"success": True, **(result or {}), "message": "保存位置已删除"})

        return guarded(run)

    @protected
    def save_routes() -> Any:
        return guarded(lambda: jsonify(service.save_routes(json_payload())))

    @protected
    def preflight() -> Any:
        return guarded(lambda: jsonify(service.preflight()))

    blueprint.add_url_rule("/api/admin/storage-setup", "snapshot", snapshot, methods=["GET"])
    blueprint.add_url_rule("/api/admin/storage-setup/accounts", "save_account", save_account, methods=["POST"])
    blueprint.add_url_rule("/api/admin/storage-setup/openlist", "save_openlist", save_openlist, methods=["PUT"])
    blueprint.add_url_rule(
        "/api/admin/storage-setup/openlist/<connection_id>",
        "delete_openlist",
        delete_openlist,
        methods=["DELETE"],
    )
    blueprint.add_url_rule("/api/admin/storage-setup/locations", "create_location", create_location, methods=["POST"])
    blueprint.add_url_rule(
        "/api/admin/storage-setup/locations/<destination_id>",
        "update_location",
        update_location,
        methods=["PATCH"],
    )
    blueprint.add_url_rule(
        "/api/admin/storage-setup/locations/<destination_id>",
        "delete_location",
        delete_location,
        methods=["DELETE"],
    )
    blueprint.add_url_rule("/api/admin/storage-setup/routes", "save_routes", save_routes, methods=["PUT"])
    blueprint.add_url_rule("/api/admin/storage-setup/preflight", "preflight", preflight, methods=["POST"])
    return blueprint


__all__ = ["StorageSetupBlueprintContext", "create_storage_setup_blueprint"]
