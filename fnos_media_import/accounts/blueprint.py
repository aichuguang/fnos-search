"""Provider 账号管理 API（docs/V1.0多链路设计文档.md §16.1）。

镜像 ``fnos_media_import/blueprints/settings.py`` 的 factory 模式：context 持有
handler 与 ``admin_required`` 装饰器；``accounts_blueprint_for_service`` 提供
开箱即用的 service 接线（app.py 未来一行接入）。

API 永不返回密文/明文凭证，只返回 ``configured``、脱敏 identity 与检测状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, request

from .crypto import MasterKeyError
from .repository import (
    AccountNotFoundError,
    ProviderAccountError,
    SelectionChangedError,
)
from .service import (
    ImmutableFieldError,
    InvalidConfigError,
    ProviderAccountService,
    provider_catalog,
)

Handler = Callable[..., Any]
Protected = Callable[[Handler], Handler]

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
        "/api/admin/providers/catalog",
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
    service: ProviderAccountService, admin_required: Protected
) -> Blueprint:
    context = AccountsRouteContext(
        admin_required=admin_required,
        catalog=_catalog_handler(),
        list_accounts=_list_accounts_handler(service),
        create_account=_create_account_handler(service),
        get_account=_get_account_handler(service),
        update_account=_update_account_handler(service),
        test_account=_test_account_handler(service),
        activate_account=_activate_account_handler(service),
        disable_account=_disable_account_handler(service),
        delete_account=_delete_account_handler(service),
    )
    return create_accounts_blueprint(context)


def _catalog_handler() -> Handler:
    def _handler() -> tuple[dict[str, Any], int]:
        return {"success": True, "providers": provider_catalog()}, 200

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
            account = service.create_account(
                provider_kind=payload.get("provider_kind") or "",
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
