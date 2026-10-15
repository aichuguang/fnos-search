"""V1.0 存储目标与入库规则管理 API（§16.3）。

路由：
    GET    /api/admin/storage-targets
    POST   /api/admin/storage-targets
    GET    /api/admin/storage-targets/{destination_id}
    PATCH  /api/admin/storage-targets/{destination_id}
    POST   /api/admin/storage-targets/{destination_id}/test
    POST   /api/admin/storage-targets/{destination_id}/disable
    DELETE /api/admin/storage-targets/{destination_id}
    GET    /api/admin/destination-rules
    PUT    /api/admin/destination-rules
    GET    /api/admin/transfer-matrix

所有路由都要求管理员认证；响应永不包含任何凭证/密钥字段（目标与规则模型本身
不含 secret）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, jsonify, request

from .assignment_service import AssignmentError, SourceAssignmentService, assignment_to_dict
from .target_service import (
    StorageTargetService,
    TargetInUseError,
    TargetConflictError,
    TargetNotFoundError,
    TargetRevisionNotFoundError,
    TargetValidationError,
    decode_categories,
    target_to_dict,
)

_CREATE_FIELDS = (
    "destination_id",
    "name",
    "provider_kind",
    "target_kind",
    "account_scope",
    "audience",
    "allow_mount_root",
)


@dataclass(frozen=True)
class StorageBlueprintContext:
    """与 app.py 其他蓝图一致的上下文注入。"""

    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    target_service: StorageTargetService
    assignment_service: SourceAssignmentService


def create_storage_blueprint(context: StorageBlueprintContext) -> Blueprint:
    blueprint = Blueprint("storage_v1", __name__)
    protected = context.admin_required
    target_service = context.target_service
    assignment_service = context.assignment_service

    def _extract_categories(payload: dict[str, Any]) -> dict[str, Any]:
        raw = payload.get("categories")
        if not raw:
            return {}
        return decode_categories(raw)

    @protected
    def _list_targets() -> Any:
        targets = target_service.list_targets()
        return jsonify({"success": True, "targets": [target_to_dict(t) for t in targets]})

    @protected
    def _create_target() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            categories = _extract_categories(payload)
            kwargs = {key: payload[key] for key in _CREATE_FIELDS if key in payload}
            target = target_service.create_target(
                categories=categories,
                account_id=payload.get("account_id"),
                binding_locator=payload.get("binding_locator") or {},
                **kwargs,
            )
        except TargetValidationError as exc:
            return jsonify({"success": False, "code": "TARGET_INVALID", "message": str(exc)}), 400
        return jsonify({"success": True, "target": target_to_dict(target)}), 201

    @protected
    def _get_target(destination_id: str) -> Any:
        try:
            target = target_service.get_target(destination_id)
        except TargetRevisionNotFoundError as exc:
            return jsonify({"success": False, "code": "TARGET_REVISION_NOT_FOUND", "message": str(exc)}), 404
        if target is None:
            return jsonify({"success": False, "code": "TARGET_NOT_FOUND", "message": "目标不存在"}), 404
        return jsonify(
            {
                "success": True,
                "target": target_to_dict(target),
                "revisions": target_service.list_revisions(destination_id),
            }
        )

    @protected
    def _revise_target(destination_id: str) -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            raw_categories = payload.get("categories")
            categories = decode_categories(raw_categories) if raw_categories else None
            target = target_service.revise_target(
                destination_id,
                name=payload.get("name"),
                audience=payload.get("audience"),
                categories=categories,
                allow_mount_root=bool(payload.get("allow_mount_root", False)),
                account_id=payload.get("account_id") if "account_id" in payload else None,
                binding_locator=payload.get("binding_locator") if "binding_locator" in payload else None,
                expected_revision=payload.get("expected_revision", payload.get("revision")),
            )
        except TargetNotFoundError as exc:
            return jsonify({"success": False, "code": "TARGET_NOT_FOUND", "message": str(exc)}), 404
        except TargetConflictError as exc:
            return jsonify({"success": False, "code": "TARGET_CONFLICT", "message": str(exc)}), 409
        except TargetValidationError as exc:
            return jsonify({"success": False, "code": "TARGET_INVALID", "message": str(exc)}), 400
        return jsonify({"success": True, "target": target_to_dict(target)})

    @protected
    def _test_target(destination_id: str) -> Any:
        try:
            result = target_service.test_target(destination_id)
        except TargetNotFoundError as exc:
            return jsonify({"success": False, "code": "TARGET_NOT_FOUND", "message": str(exc)}), 404
        return jsonify({"success": True, **result})

    @protected
    def _disable_target(destination_id: str) -> Any:
        try:
            target = target_service.disable_target(destination_id)
        except TargetNotFoundError as exc:
            return jsonify({"success": False, "code": "TARGET_NOT_FOUND", "message": str(exc)}), 404
        return jsonify({"success": True, "target": target_to_dict(target)})

    @protected
    def _delete_target(destination_id: str) -> Any:
        try:
            target_service.delete_target(destination_id)
        except TargetNotFoundError as exc:
            return jsonify({"success": False, "code": "TARGET_NOT_FOUND", "message": str(exc)}), 404
        except TargetInUseError as exc:
            return jsonify({"success": False, "code": "TARGET_IN_USE", "message": str(exc)}), 409
        return jsonify({"success": True})

    @protected
    def _list_assignments() -> Any:
        snapshot = assignment_service.snapshot()
        return jsonify({
            "success": True, "assignments": [assignment_to_dict(item) for item in snapshot["assignments"]],
            "routing_token": snapshot["routing_token"],
        })

    @protected
    def _put_assignments() -> Any:
        payload = request.get_json(silent=True) or {}
        assignments = payload.get("assignments") if isinstance(payload, dict) else None
        if not isinstance(assignments, list):
            return jsonify({"success": False, "code": "ASSIGNMENTS_INVALID", "message": "assignments 必须是数组"}), 400
        try:
            saved = assignment_service.replace(
                assignments, expected_token=payload.get("routing_token"), require_snapshot=True,
            )
        except TargetNotFoundError as exc:
            return jsonify({"success": False, "code": "TARGET_NOT_FOUND", "message": str(exc)}), 404
        except AssignmentError as exc:
            return jsonify({"success": False, "code": exc.code, "message": str(exc)}), getattr(exc, "status", 400)
        except TargetValidationError as exc:
            return jsonify({"success": False, "code": "ASSIGNMENTS_INVALID", "message": str(exc)}), 400
        return jsonify({"success": True, "assignments": [assignment_to_dict(item) for item in saved]})

    @protected
    def _transfer_matrix() -> Any:
        return jsonify(assignment_service.matrix())

    @protected
    def _list_bindings(destination_id: str) -> Any:
        try:
            bindings = target_service.list_bindings(destination_id)
        except TargetNotFoundError as exc:
            return jsonify({"success": False, "code": "TARGET_NOT_FOUND", "message": str(exc)}), 404
        return jsonify({
            "success": True,
            "bindings": [
                {
                    "binding_id": item.binding_id,
                    "target_revision": item.target_revision,
                    "binding_revision": item.current_revision,
                    "provider_kind": item.provider_kind,
                    "account_id": item.account_id,
                    "account_revision": item.account_revision,
                    "locator": item.locator,
                    "enabled": item.enabled,
                }
                for item in bindings
            ],
        })

    blueprint.add_url_rule(
        "/api/admin/storage-targets",
        endpoint="storage_targets_list",
        view_func=_list_targets,
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/admin/storage-targets",
        endpoint="storage_targets_create",
        view_func=_create_target,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/admin/storage-targets/<destination_id>",
        endpoint="storage_target_detail",
        view_func=_get_target,
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/admin/storage-targets/<destination_id>",
        endpoint="storage_target_revise",
        view_func=_revise_target,
        methods=["PATCH"],
    )
    blueprint.add_url_rule(
        "/api/admin/storage-targets/<destination_id>/test",
        endpoint="storage_target_test",
        view_func=_test_target,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/admin/storage-targets/<destination_id>/disable",
        endpoint="storage_target_disable",
        view_func=_disable_target,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/admin/storage-targets/<destination_id>",
        endpoint="storage_target_delete",
        view_func=_delete_target,
        methods=["DELETE"],
    )
    blueprint.add_url_rule(
        "/api/admin/source-assignments",
        endpoint="source_assignments_list",
        view_func=_list_assignments,
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/admin/source-assignments",
        endpoint="source_assignments_put",
        view_func=_put_assignments,
        methods=["PUT"],
    )
    blueprint.add_url_rule(
        "/api/admin/routing-matrix",
        endpoint="routing_matrix",
        view_func=_transfer_matrix,
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/admin/storage-targets/<destination_id>/bindings",
        endpoint="storage_target_bindings",
        view_func=_list_bindings,
        methods=["GET"],
    )
    return blueprint
