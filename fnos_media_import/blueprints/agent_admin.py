"""Admin routes for Agent tokens."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, jsonify, request


@dataclass(frozen=True)
class AgentAdminRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    token_store: Callable[[], Any]
    audit: Callable[[], Any]
    token_profiles: dict[str, tuple[str, ...]]
    all_scopes: tuple[str, ...]


def create_agent_admin_blueprint(context: AgentAdminRouteContext) -> Blueprint:
    blueprint = Blueprint("agent_admin", __name__)

    def _store():
        store = context.token_store()
        return store

    def _require_store():
        store = _store()
        if store is None:
            return None
        return store

    @blueprint.get("/api/admin/agent/tokens")
    @context.admin_required
    def list_tokens():
        store = _require_store()
        if store is None:
            return jsonify({"success": False, "message": "Agent 接口未启用"}), 409
        return jsonify(
            {
                "success": True,
                "items": store.list_tokens(),
                "profiles": {name: sorted(scopes) for name, scopes in context.token_profiles.items()},
                "scopes": list(context.all_scopes),
            }
        )

    @blueprint.post("/api/admin/agent/tokens")
    @context.admin_required
    def create_token():
        store = _require_store()
        if store is None:
            return jsonify({"success": False, "message": "Agent 接口未启用"}), 409
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name") or "").strip()
        if not name:
            return jsonify({"success": False, "message": "请填写令牌名称"}), 400
        profile = str(payload.get("profile") or "read").strip()
        raw_scopes = payload.get("scopes")
        if isinstance(raw_scopes, list) and raw_scopes:
            scopes = [str(item).strip() for item in raw_scopes if str(item).strip()]
        else:
            scopes = list(context.token_profiles.get(profile) or context.token_profiles.get("read") or ())
        unknown = sorted(set(scopes) - set(context.all_scopes))
        if unknown:
            return jsonify({"success": False, "message": f"未知作用域：{', '.join(unknown)}"}), 400
        plaintext, record = store.create(
            name,
            scopes,
            rate_limit=int(payload.get("rate_limit") or 0),
            expires_at=str(payload.get("expires_at") or ""),
        )
        return jsonify({"success": True, "token": plaintext, "item": record, "message": "令牌已创建，请立刻保存"})

    @blueprint.delete("/api/admin/agent/tokens/<int:token_id>")
    @context.admin_required
    def revoke_token(token_id: int):
        store = _require_store()
        if store is None:
            return jsonify({"success": False, "message": "Agent 接口未启用"}), 409
        if store.revoke(int(token_id)):
            return jsonify({"success": True, "message": "令牌已撤销"})
        return jsonify({"success": False, "message": "令牌不存在或已撤销"}), 404

    @blueprint.get("/api/admin/agent/audit")
    @context.admin_required
    def audit_log():
        log = context.audit()
        if log is None:
            return jsonify({"success": False, "message": "Agent 接口未启用"}), 409
        limit = max(1, min(int(request.args.get("limit") or 30), 200))
        return jsonify({"success": True, "items": log.recent(limit=limit)})

    return blueprint
