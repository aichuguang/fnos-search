from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, jsonify, request, session


@dataclass(frozen=True)
class AuthRouteContext:
    csrf_enabled: Callable[[], bool]
    rate_limit_login: Callable[[], Any]
    verify_password: Callable[[str, str], tuple[bool, str]]
    admin_profile: Callable[[], dict[str, Any]]
    is_logged_in: Callable[[], bool]
    security_status: Callable[[], dict[str, Any]]
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]


def create_auth_blueprint(context: AuthRouteContext) -> Blueprint:
    blueprint = Blueprint("admin_auth", __name__)

    @blueprint.before_app_request
    def verify_admin_csrf():
        if not context.csrf_enabled():
            return None
        if request.method in {"GET", "HEAD", "OPTIONS"} or not request.path.startswith("/api/admin/"):
            return None
        if request.path in {"/api/admin/login", "/api/admin/logout"}:
            return None
        expected = str(session.get("csrf_token") or "")
        supplied = str(request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            return jsonify({"success": False, "message": "CSRF token 校验失败"}), 403
        return None

    @blueprint.post("/api/admin/login")
    def login():
        limited = context.rate_limit_login()
        if limited:
            return limited
        payload = request.get_json(silent=True) or {}
        ok, expected_username = context.verify_password(str(payload.get("username") or "").strip(), str(payload.get("password") or ""))
        if not ok:
            return jsonify({"success": False, "message": "用户名或密码错误"}), 401
        session.clear()
        session["admin_logged_in"] = True
        session["admin_username"] = expected_username
        session["csrf_token"] = secrets.token_urlsafe(32)
        session.permanent = True
        return jsonify({"success": True, "message": "登录成功", "user": context.admin_profile()})

    @blueprint.post("/api/admin/logout")
    def logout():
        session.clear()
        return jsonify({"success": True, "message": "已退出登录"})

    @blueprint.get("/api/admin/session")
    def admin_session():
        logged_in = context.is_logged_in()
        return jsonify({"success": True, "logged_in": logged_in, "user": context.admin_profile() if logged_in else None, "csrf_token": str(session.get("csrf_token") or "") if logged_in else ""})

    @blueprint.get("/api/admin/security/status")
    @context.admin_required
    def security_status():
        return jsonify(context.security_status())

    return blueprint
