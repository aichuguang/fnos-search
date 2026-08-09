"""Provider 账号管理 API（docs/V1.0多链路设计文档.md §16.1）。

镜像 ``fnos_media_import/blueprints/settings.py`` 的 factory 模式：context 持有
handler 与 ``admin_required`` 装饰器；``accounts_blueprint_for_service`` 提供
开箱即用的 service 接线（app.py 未来一行接入）。

API 永不返回密文/明文凭证，只返回 ``configured``、脱敏 identity 与检测状态。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import secrets
import time
from typing import Any, Callable

from flask import Blueprint, request

from .crypto import MasterKeyError
from .auth_sessions import AuthSessionError, ProviderAuthSessionService, _safe_provider_message
from .repository import (
    AccountNotFoundError,
    ProviderAccountError,
    SelectionChangedError,
)
from .service import (
    ImmutableFieldError,
    InvalidConfigError,
    ProviderAccountService,
)

Handler = Callable[..., Any]
Protected = Callable[[Handler], Handler]

logger = logging.getLogger("fnos_media_import.provider_auth")

_ERROR_STATUS: dict[str, int] = {
    "ACCOUNT_NOT_FOUND": 404,
    "INVALID_CONFIG": 400,
    "IMMUTABLE_FIELD": 400,
    "ACCOUNT_CONFLICT": 409,
    "ACCOUNT_DISABLED": 409,
    "ACCOUNT_NOT_TESTED": 409,
    "ACCOUNT_ACTIVE": 409,
    "ACCOUNT_IN_USE": 409,
    "PROVIDER_MISMATCH": 409,
    "SELECTION_CHANGED": 409,
}

_AUTH_ERROR_STATUS: dict[str, int] = {
    "AUTH_SESSION_NOT_FOUND": 404,
    "AUTH_SESSION_CONFLICT": 409,
    "AUTH_SESSION_BUSY": 409,
}


def _raise_exception(exc: BaseException) -> None:
    """Re-raise a parsed request error inside the shared response guard."""

    raise exc


def _error(exc: BaseException) -> tuple[dict[str, Any], int]:
    code = str(getattr(exc, "code", "ACCOUNT_ERROR"))
    return (
        {"success": False, "code": code, "message": str(exc)},
        _ERROR_STATUS.get(code, 409),
    )


def _guarded(fn: Callable[[], tuple[Any, int]]) -> tuple[Any, int]:
    try:
        return fn()
    except ProviderAccountError as exc:
        return _error(exc)
    except MasterKeyError as exc:
        return (
            {"success": False, "code": "MASTER_KEY_UNAVAILABLE", "message": str(exc)},
            500,
        )


@dataclass(frozen=True)
class AccountsRouteContext:
    admin_required: Protected
    catalog: Handler = lambda: ({"success": True}, 200)
    list_accounts: Handler = lambda: ({"success": True}, 200)
    create_account: Handler = lambda: ({"success": True}, 200)
    get_account: Handler = lambda: ({"success": True}, 200)
    update_account: Handler = lambda: ({"success": True}, 200)
    test_account: Handler = lambda: ({"success": True}, 200)
    activate_account: Handler = lambda: ({"success": True}, 200)
    disable_account: Handler = lambda: ({"success": True}, 200)
    delete_account: Handler = lambda: ({"success": True}, 200)


def create_accounts_blueprint(context: AccountsRouteContext) -> Blueprint:
    blueprint = Blueprint("accounts", __name__)
    protected = context.admin_required
    blueprint.add_url_rule(
        "/api/admin/providers",
        endpoint="catalog",
        view_func=protected(context.catalog),
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/admin/provider-accounts",
        endpoint="list_accounts",
        view_func=protected(context.list_accounts),
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/admin/provider-accounts",
        endpoint="create_account",
        view_func=protected(context.create_account),
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/admin/provider-accounts/<string:account_id>",
        endpoint="get_account",
        view_func=protected(context.get_account),
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/admin/provider-accounts/<string:account_id>",
        endpoint="update_account",
        view_func=protected(context.update_account),
        methods=["PATCH"],
    )
    blueprint.add_url_rule(
        "/api/admin/provider-accounts/<string:account_id>/test",
        endpoint="test_account",
        view_func=protected(context.test_account),
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/admin/provider-accounts/<string:account_id>/activate",
        endpoint="activate_account",
        view_func=protected(context.activate_account),
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/admin/provider-accounts/<string:account_id>/disable",
        endpoint="disable_account",
        view_func=protected(context.disable_account),
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/admin/provider-accounts/<string:account_id>",
        endpoint="delete_account",
        view_func=protected(context.delete_account),
        methods=["DELETE"],
    )
    return blueprint


# ---- service 接线（app.py 集成时可直接使用） ----


def accounts_blueprint_for_service(
    service: ProviderAccountService,
    admin_required: Protected,
    auth_sessions: ProviderAuthSessionService | None = None,
) -> Blueprint:
    context = AccountsRouteContext(
        admin_required=admin_required,
        catalog=_catalog_handler(service),
        list_accounts=_list_accounts_handler(service),
        create_account=_create_account_handler(service),
        get_account=_get_account_handler(service),
        update_account=_update_account_handler(service),
        test_account=_test_account_handler(service),
        activate_account=_activate_account_handler(service),
        disable_account=_disable_account_handler(service),
        delete_account=_delete_account_handler(service),
    )
    blueprint = create_accounts_blueprint(context)
    if auth_sessions is not None:
        _register_auth_session_routes(blueprint, auth_sessions, admin_required)
    return blueprint


def _register_auth_session_routes(
    blueprint: Blueprint,
    service: ProviderAuthSessionService,
    admin_required: Protected,
) -> None:
    def guarded(
        callback: Callable[[], Any],
        *,
        provider_kind: str = "",
        auth_method: str = "",
        action: str = "",
    ) -> tuple[Any, int]:
        trace_id = str(request.headers.get("X-Request-ID") or f"auth_{secrets.token_hex(8)}")[:80]
        started = time.monotonic()
        try:
            value = callback()
            logger.info(
                "event=provider_auth_succeeded trace_id=%s provider=%s method=%s action=%s duration_ms=%d",
                trace_id,
                provider_kind or "unknown",
                auth_method or "unknown",
                action or "unknown",
                int((time.monotonic() - started) * 1000),
            )
            return {"success": True, "session": value, "trace_id": trace_id}, 200
        except AuthSessionError as exc:
            code = str(getattr(exc, "code", "") or "AUTH_SESSION_INVALID")
            status = _AUTH_ERROR_STATUS.get(code, 400)
            effective_provider = provider_kind or str(getattr(exc, "provider_kind", "") or "")
            effective_method = auth_method or str(getattr(exc, "auth_method", "") or "")
            logger.warning(
                "event=provider_auth_failed trace_id=%s provider=%s method=%s action=%s stage=%s code=%s http_status=%s duration_ms=%d message=%s",
                trace_id,
                effective_provider or "unknown",
                effective_method or "unknown",
                action or "unknown",
                str(getattr(exc, "stage", "") or "unknown"),
                code,
                getattr(exc, "http_status", None) or "-",
                int((time.monotonic() - started) * 1000),
                _safe_provider_message(exc),
            )
            payload: dict[str, Any] = {
                "success": False,
                "code": code,
                "message": _safe_provider_message(exc),
                "trace_id": trace_id,
                # These fields are deliberately coarse and contain no
                # credentials.  Returning them lets the UI distinguish a
                # login-form, captcha, session-exchange, or account-probe
                # failure without forcing an operator to grep Werkzeug lines.
                "stage": str(getattr(exc, "stage", "") or "unknown"),
                "provider_kind": effective_provider or None,
                "auth_method": effective_method or None,
                "http_status": getattr(exc, "http_status", None),
                "retryable": bool(getattr(exc, "retryable", False)),
            }
            field_errors = dict(getattr(exc, "field_errors", {}) or {})
            if field_errors:
                payload["field_errors"] = field_errors
            return payload, status
        except ProviderAccountError as exc:
            code = str(getattr(exc, "code", "") or "ACCOUNT_ERROR")
            status = _ERROR_STATUS.get(code, 409)
            logger.warning(
                "event=provider_auth_failed trace_id=%s provider=%s method=%s action=%s stage=account code=%s http_status=%s duration_ms=%d message=%s",
                trace_id,
                provider_kind or "unknown",
                auth_method or "unknown",
                action or "unknown",
                code,
                status,
                int((time.monotonic() - started) * 1000),
                _safe_provider_message(exc),
            )
            return {
                "success": False,
                "code": code,
                "message": _safe_provider_message(exc),
                "trace_id": trace_id,
                "stage": "account",
                "provider_kind": provider_kind or None,
                "auth_method": auth_method or None,
                "retryable": code in {"ACCOUNT_CONFLICT", "SELECTION_CHANGED"},
            }, status
        except MasterKeyError as exc:
            logger.error(
                "event=provider_auth_failed trace_id=%s provider=%s method=%s action=%s stage=crypto code=MASTER_KEY_UNAVAILABLE duration_ms=%d",
                trace_id,
                provider_kind or "unknown",
                auth_method or "unknown",
                action or "unknown",
                int((time.monotonic() - started) * 1000),
            )
            return {
                "success": False,
                "code": "MASTER_KEY_UNAVAILABLE",
                "message": "账号加密主密钥不可用，请先配置后重试",
                "trace_id": trace_id,
                "stage": "crypto",
                "retryable": False,
            }, 500
        except Exception:  # noqa: BLE001
            logger.exception(
                "event=provider_auth_failed trace_id=%s provider=%s method=%s action=%s stage=unexpected duration_ms=%d",
                trace_id,
                provider_kind or "unknown",
                auth_method or "unknown",
                action or "unknown",
                int((time.monotonic() - started) * 1000),
            )
            return {
                "success": False,
                "code": "AUTH_SESSION_FAILED",
                "message": "登录处理失败，请根据 trace_id 查看服务日志",
                "trace_id": trace_id,
                "stage": "unexpected",
                "retryable": False,
            }, 500

    def json_payload(*, required: bool) -> dict[str, Any]:
        has_body = bool(request.get_data(cache=True))
        if not has_body and not required:
            return {}
        if not request.is_json:
            error = AuthSessionError(
                "请求体必须是 JSON 对象",
                code="INVALID_JSON",
                stage="request",
            )
            error.field_errors = {"body": "必须是 JSON 对象"}
            raise error
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            error = AuthSessionError(
                "请求体必须是 JSON 对象",
                code="INVALID_JSON",
                stage="request",
            )
            error.field_errors = {"body": "必须是 JSON 对象"}
            raise error
        return payload

    @admin_required
    def start_auth_session() -> tuple[Any, int]:
        try:
            payload = json_payload(required=True)
            provider_kind = str(payload.get("provider_kind") or "").strip()
            auth_method = str(payload.get("auth_method") or "").strip()
            field_errors: dict[str, str] = {}
            if not provider_kind:
                field_errors["provider_kind"] = "必填"
            if not auth_method:
                field_errors["auth_method"] = "必填"
            if field_errors:
                error = AuthSessionError(
                    "登录请求缺少 Provider 或登录方式",
                    code="AUTH_INPUT_REQUIRED",
                    stage="request",
                )
                error.field_errors = field_errors
                raise error
            auth_payload = payload.get("payload", {})
            if auth_payload is None:
                auth_payload = {}
            if not isinstance(auth_payload, dict):
                error = AuthSessionError(
                    "payload 必须是 JSON 对象",
                    code="AUTH_INPUT_INVALID",
                    stage="request",
                )
                error.field_errors = {"payload": "必须是 JSON 对象"}
                raise error
        except AuthSessionError as exc:
            return guarded(
                lambda error=exc: _raise_exception(error),
                provider_kind=str(locals().get("provider_kind") or ""),
                auth_method=str(locals().get("auth_method") or ""),
                action="start",
            )
        return guarded(
            lambda: service.start(
                provider_kind,
                auth_method,
                auth_payload,
            ),
            provider_kind=provider_kind,
            auth_method=auth_method,
            action="start",
        )

    @admin_required
    def get_auth_session(session_id: str) -> tuple[Any, int]:
        return guarded(lambda: service.get(session_id), action="get")

    @admin_required
    def poll_auth_session(session_id: str) -> tuple[Any, int]:
        try:
            payload = json_payload(required=False)
        except AuthSessionError as exc:
            return guarded(
                lambda error=exc: _raise_exception(error),
                action="poll",
            )
        return guarded(lambda: service.poll(session_id, payload), action="poll")

    @admin_required
    def cancel_auth_session(session_id: str) -> tuple[Any, int]:
        return guarded(lambda: service.cancel(session_id), action="cancel")

    blueprint.add_url_rule(
        "/api/admin/provider-auth-sessions",
        "start_auth_session",
        start_auth_session,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/admin/provider-auth-sessions/<string:session_id>",
        "get_auth_session",
        get_auth_session,
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/admin/provider-auth-sessions/<string:session_id>/verify",
        "poll_auth_session",
        poll_auth_session,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/admin/provider-auth-sessions/<string:session_id>",
        "cancel_auth_session",
        cancel_auth_session,
        methods=["DELETE"],
    )


def _catalog_handler(service: ProviderAccountService) -> Handler:
    def _handler() -> tuple[dict[str, Any], int]:
        return {"success": True, "providers": service.catalog()}, 200

    return _handler


def _list_accounts_handler(service: ProviderAccountService) -> Handler:
    def _handler() -> tuple[dict[str, Any], int]:
        provider_kind = request.args.get("provider_kind") or None
        accounts = service.list_accounts(provider_kind)
        return {
            "success": True,
            "accounts": [service.serialize_account(account) for account in accounts],
            "active_selections": service.list_active_selections(),
        }, 200

    return _handler


def _create_account_handler(service: ProviderAccountService) -> Handler:
    def _handler() -> tuple[Any, int]:
        payload = request.get_json(silent=True) or {}

        def _run() -> tuple[Any, int]:
            provider_kind = str(payload.get("provider_kind") or "").strip()
            provider = next(
                (
                    item
                    for item in service.catalog()
                    if item.get("provider_kind") == provider_kind
                ),
                None,
            )
            if provider and provider.get("auth_methods"):
                raise InvalidConfigError("该网盘必须通过登录授权添加账号，不能手工提交凭证")
            account = service.create_account(
                provider_kind=provider_kind,
                name=payload.get("name") or "",
                config=payload.get("config") or {},
                credentials=payload.get("credentials") or {},
                identity_hint=payload.get("identity_hint") or "",
                enabled=bool(payload.get("enabled", True)),
            )
            return {"success": True, "account": service.serialize_account(account)}, 200

        return _guarded(_run)

    return _handler


def _get_account_handler(service: ProviderAccountService) -> Handler:
    def _handler(account_id: str) -> tuple[Any, int]:
        def _run() -> tuple[Any, int]:
            account = service.get_account(account_id)
            if account is None:
                raise AccountNotFoundError(account_id)
            selection = service.get_active_account(account.provider_kind)
            is_active = selection is not None and selection.account_id == account_id
            return {
                "success": True,
                "account": service.serialize_account(account),
                "config": service.safe_account_config(account_id),
                "is_active": is_active,
            }, 200

        return _guarded(_run)

    return _handler


def _update_account_handler(service: ProviderAccountService) -> Handler:
    def _handler(account_id: str) -> tuple[Any, int]:
        payload = request.get_json(silent=True) or {}

        def _run() -> tuple[Any, int]:
            forbidden = set(payload) & {
                "account_id",
                "provider_kind",
                "revision",
                "current_revision",
                "created_at",
            }
            if forbidden:
                raise ImmutableFieldError(f"不可修改字段：{', '.join(sorted(forbidden))}")
            unknown = set(payload) - {"name", "config", "credentials", "identity_hint"}
            if unknown:
                raise InvalidConfigError(f"不支持的字段：{', '.join(sorted(unknown))}")
            current_account = service.get_account(account_id)
            if current_account is None:
                raise AccountNotFoundError(account_id)
            if "config" in payload or "credentials" in payload:
                provider = next(
                    (
                        item
                        for item in service.catalog()
                        if item.get("provider_kind") == current_account.provider_kind
                    ),
                    None,
                )
                if provider and provider.get("auth_methods"):
                    raise InvalidConfigError(
                        "账号凭证只能由登录会话或 Provider 自动刷新"
                    )
            if "name" in payload:
                service.update_account_name(account_id, payload["name"])
            if "config" in payload or "credentials" in payload:
                service.revise_credentials(
                    account_id,
                    config=payload.get("config") or {},
                    credentials=payload.get("credentials") or {},
                    identity_hint=payload.get("identity_hint") or "",
                )
            account = service.get_account(account_id)
            if account is None:
                raise AccountNotFoundError(account_id)
            return {"success": True, "account": service.serialize_account(account)}, 200

        return _guarded(_run)

    return _handler


def _test_account_handler(service: ProviderAccountService) -> Handler:
    def _handler(account_id: str) -> tuple[Any, int]:
        def _run() -> tuple[Any, int]:
            result = service.test_account(account_id)
            return {
                "success": True,
                "result": {
                    "ok": result.ok,
                    "identity_hint": result.identity_hint,
                    "message": result.message,
                    "errors": result.errors,
                },
            }, 200

        return _guarded(_run)

    return _handler


def _activate_account_handler(service: ProviderAccountService) -> Handler:
    def _handler(account_id: str) -> tuple[Any, int]:
        payload = request.get_json(silent=True) or {}

        def _run() -> tuple[Any, int]:
            scope = payload.get("scope") or "default"
            expected = payload.get("expected_selection_revision")
            if expected is not None:
                try:
                    expected = int(expected)
                except (TypeError, ValueError) as exc:
                    raise InvalidConfigError(
                        "expected_selection_revision 必须是整数"
                    ) from exc
            selection = service.activate_account(scope, account_id, expected)
            return {
                "success": True,
                "selection": {
                    "provider_kind": selection.provider_kind,
                    "scope": selection.scope,
                    "account_id": selection.account_id,
                    "account_revision": selection.account_revision,
                    "selection_revision": selection.selection_revision,
                },
            }, 200

        return _guarded(_run)

    return _handler


def _disable_account_handler(service: ProviderAccountService) -> Handler:
    def _handler(account_id: str) -> tuple[Any, int]:
        def _run() -> tuple[Any, int]:
            service.disable_account(account_id)
            return {"success": True}, 200

        return _guarded(_run)

    return _handler


def _delete_account_handler(service: ProviderAccountService) -> Handler:
    def _handler(account_id: str) -> tuple[Any, int]:
        def _run() -> tuple[Any, int]:
            service.delete_account(account_id)
            return {"success": True}, 200

        return _guarded(_run)

    return _handler


__all__ = [
    "AccountsRouteContext",
    "accounts_blueprint_for_service",
    "create_accounts_blueprint",
]
