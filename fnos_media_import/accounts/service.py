"""ProviderAccountService：账号 CRUD、凭证修订、检测、激活与凭证解析。

规则（docs/V1.0多链路设计文档.md §5）：
- 每次凭证更新创建不可变修订，绝不覆盖旧密文。
- 当前账号不能直接禁用/删除，必须先切换到其他账号。
- 未启用或检测未通过的账号不能激活。
- account_id / provider_kind 不可修改。
- API 永不返回密文/明文，只返回 configured 标记、脱敏 identity 与检测状态。
"""

from __future__ import annotations

import copy
import json
import secrets
from typing import Any, Callable

from ..domain.models import AccountSelection, AccountTestResult, ProviderAccount
from ..time_utils import utc_now_iso as utc_now
from .crypto import MasterKeyError, decrypt, encrypt
from .repository import (
    AccountInUseError,
    AccountNotFoundError,
    ProviderAccountError,
    ProviderAccountRepository,
)

TEST_STATUS_UNTESTED = "untested"
TEST_STATUS_OK = "ok"
TEST_STATUS_FAILED = "failed"

DEFAULT_SCOPE = "default"
ACCOUNT_ID_PREFIX = "acc_"

MasterKeyProvider = Callable[[], bytes]
AccountTester = Callable[[dict[str, Any], dict[str, Any]], AccountTestResult]


class AccountDisabledError(ProviderAccountError):
    code = "ACCOUNT_DISABLED"


class AccountNotTestedError(ProviderAccountError):
    code = "ACCOUNT_NOT_TESTED"


class ActiveAccountDisableError(ProviderAccountError):
    code = "ACCOUNT_ACTIVE"


class InvalidConfigError(ProviderAccountError):
    code = "INVALID_CONFIG"


class ImmutableFieldError(ProviderAccountError):
    code = "IMMUTABLE_FIELD"


class CredentialResolutionError(ProviderAccountError):
    code = "CREDENTIAL_RESOLUTION"


class ProviderMismatchError(ProviderAccountError):
    code = "PROVIDER_MISMATCH"


# ---- Provider Catalog（V1.0 四个 Provider 的能力与账号表单 Schema）----
# 后续 Adapter 落地后由 Provider Registry 提供，这里作为地基占位。

PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    "mobile": {
        "display_name": "移动云",
        "capabilities": {
            "supports_search": True,
            "supports_share_inspection": True,
            "supports_native_save": True,
            "supports_native_offline": False,
            "supports_operation_polling": True,
            "supports_native_verify": True,
            "supports_relay_upload": True,
        },
        "schema": {
            "fields": [
                {
                    "name": "cookie",
                    "label": "Cookie",
                    "secret": True,
                    "required": True,
                    "help": "移动云浏览器登录后的 Cookie",
                },
                {
                    "name": "phone",
                    "label": "手机号",
                    "secret": False,
                    "required": False,
                    "help": "用于核对账号身份",
                },
            ]
        },
    },
    "quark": {
        "display_name": "夸克",
        "capabilities": {
            "supports_search": True,
            "supports_share_inspection": True,
            "supports_native_save": True,
            "supports_native_offline": False,
            "supports_operation_polling": True,
            "supports_native_verify": True,
            "supports_relay_upload": False,
        },
        "schema": {
            "fields": [
                {
                    "name": "cookie",
                    "label": "Cookie",
                    "secret": True,
                    "required": True,
                    "help": "夸克网盘浏览器登录后的 Cookie",
                }
            ]
        },
    },
    "tianyi": {
        "display_name": "天翼云",
        "capabilities": {
            "supports_search": True,
            "supports_share_inspection": True,
            "supports_native_save": True,
            "supports_native_offline": False,
            "supports_operation_polling": True,
            "supports_native_verify": True,
            "supports_relay_upload": False,
        },
        "schema": {
            "fields": [
                {
                    "name": "cookie",
                    "label": "Cookie",
                    "secret": True,
                    "required": True,
                    "help": "天翼云盘浏览器登录后的 Cookie",
                }
            ]
        },
    },
    "sixpan": {
        "display_name": "六盘",
        "capabilities": {
            "supports_search": False,
            "supports_share_inspection": False,
            "supports_native_save": False,
            "supports_native_offline": True,
            "supports_operation_polling": True,
            "supports_native_verify": True,
            "supports_relay_upload": False,
        },
        "schema": {
            "fields": [
                {
                    "name": "cookie",
                    "label": "Cookie",
                    "secret": True,
                    "required": True,
                    "help": "六盘登录凭证（磁链离线需要）",
                }
            ]
        },
    },
}


def provider_catalog() -> list[dict[str, Any]]:
    """返回深拷贝的 Provider 目录，避免调用方改动模块常量。"""
    return [
        {
            "provider_kind": kind,
            "display_name": info["display_name"],
            "capabilities": copy.deepcopy(info["capabilities"]),
            "schema": copy.deepcopy(info["schema"]),
        }
        for kind, info in PROVIDER_CATALOG.items()
    ]


def _mask_identity(value: str) -> str:
    value = str(value or "").strip()
    if len(value) <= 3:
        return value
    if len(value) <= 7:
        return value[0] + "*" * (len(value) - 2) + value[-1]
    return value[:3] + "****" + value[-4:]


class ProviderAccountService:
    def __init__(
        self,
        database: Any,
        master_key_provider: MasterKeyProvider,
        *,
        tester: AccountTester | None = None,
    ) -> None:
        self._database = database
        self._repository = ProviderAccountRepository(database)
        self._master_key_provider = master_key_provider
        self._default_tester = tester

    # ---- master key ----

    def _master_key(self) -> bytes:
        return self._master_key_provider()

    @staticmethod
    def _new_account_id() -> str:
        return f"{ACCOUNT_ID_PREFIX}{secrets.token_hex(8)}"

    # ---- catalog ----

    @classmethod
    def _validate_credentials(
        cls, provider_kind: str, config: dict[str, Any], credentials: dict[str, Any]
    ) -> list[str]:
        errors: list[str] = []
        fields = PROVIDER_CATALOG[provider_kind]["schema"]["fields"]
        for field in fields:
            if not field.get("required"):
                continue
            container = credentials if field.get("secret") else (config or {})
            if not str((container or {}).get(field["name"]) or "").strip():
                errors.append(f"缺少必填字段：{field['label']}")
        return errors

    @staticmethod
    def _derive_identity_hint(
        provider_kind: str, config: dict[str, Any], credentials: dict[str, Any]
    ) -> str:
        phone = str((config or {}).get("phone") or "").strip()
        if phone:
            return _mask_identity(phone)
        return ""

    @staticmethod
    def _serialize_selection(selection: AccountSelection) -> dict[str, Any]:
        return {
            "provider_kind": selection.provider_kind,
            "scope": selection.scope,
            "account_id": selection.account_id,
            "account_revision": selection.account_revision,
            "selection_revision": selection.selection_revision,
        }

    # ---- account CRUD ----

    def create_account(
        self,
        *,
        provider_kind: str,
        name: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
        identity_hint: str = "",
        enabled: bool = True,
    ) -> ProviderAccount:
        kind = str(provider_kind or "").strip()
        if kind not in PROVIDER_CATALOG:
            raise InvalidConfigError(f"不支持的 Provider：{kind}")
        errors = self._validate_credentials(kind, config or {}, credentials or {})
        if errors:
            raise InvalidConfigError("；".join(errors))
        master_key = self._master_key()
        ciphertext, key_id = encrypt(
            master_key, json.dumps(credentials or {}, ensure_ascii=False, sort_keys=True)
        )
        now = utc_now()
        hint = str(identity_hint or "").strip() or self._derive_identity_hint(
            kind, config or {}, credentials or {}
        )
        account_id = self._new_account_id()
        account = ProviderAccount(
            account_id=account_id,
            provider_kind=kind,
            name=str(name or "").strip() or kind,
            enabled=enabled,
            current_revision=1,
            identity_hint=hint,
            last_test_status=TEST_STATUS_UNTESTED,
            last_test_message="",
            last_tested_at=None,
            created_at=now,
            updated_at=now,
        )
        with self._database.connect() as conn:
            self._repository.create_account(account, conn=conn)
            self._repository.insert_revision(
                account_id,
                1,
                json.dumps(config or {}, ensure_ascii=False),
                ciphertext,
                key_id,
                conn=conn,
            )
        return account

    def get_account(self, account_id: str) -> ProviderAccount | None:
        return self._repository.get_account(account_id)

    def list_accounts(self, provider_kind: str | None = None) -> list[ProviderAccount]:
        return self._repository.list_accounts(provider_kind)

    def update_account_name(self, account_id: str, name: str) -> ProviderAccount:
        with self._database.connect() as conn:
            self._repository.update_account_name(account_id, str(name or "").strip(), conn=conn)
            account = self._repository.get_account(account_id, conn=conn)
        return account  # type: ignore[return-value]

    def revise_credentials(
        self,
        account_id: str,
        *,
        config: dict[str, Any],
        credentials: dict[str, Any],
        identity_hint: str = "",
    ) -> ProviderAccount:
        """凭证更新：创建新修订并提升 current_revision，旧修订保持可读。"""
        with self._database.connect() as conn:
            account = self._repository.get_account(account_id, conn=conn)
            if account is None:
                raise AccountNotFoundError(account_id)
            errors = self._validate_credentials(
                account.provider_kind, config or {}, credentials or {}
            )
            if errors:
                raise InvalidConfigError("；".join(errors))
            ciphertext, key_id = encrypt(
                self._master_key(),
                json.dumps(credentials or {}, ensure_ascii=False, sort_keys=True),
            )
            new_revision = account.current_revision + 1
            self._repository.insert_revision(
                account_id,
                new_revision,
                json.dumps(config or {}, ensure_ascii=False),
                ciphertext,
                key_id,
                conn=conn,
            )
            self._repository.set_account_revision(account_id, new_revision, conn=conn)
            self._repository.update_account_test(
                account_id, TEST_STATUS_UNTESTED, "", None, conn=conn
            )
            hint = str(identity_hint or "").strip() or self._derive_identity_hint(
                account.provider_kind, config or {}, credentials or {}
            )
            if hint:
                self._repository.update_identity_hint(account_id, hint, conn=conn)
            updated = self._repository.get_account(account_id, conn=conn)
        return updated  # type: ignore[return-value]

    # ---- testing ----

    @staticmethod
    def _noop_tester(credentials: dict[str, Any], config: dict[str, Any]) -> AccountTestResult:
        return AccountTestResult(ok=True, message="检测通过（未连接真实服务）")

    def test_account(
        self,
        account_id: str,
        *,
        tester: AccountTester | None = None,
    ) -> AccountTestResult:
        """执行配置校验 + 注入的网络检测 tester，并把结果写回账号。"""
        with self._database.connect() as conn:
            account = self._repository.get_account(account_id, conn=conn)
            if account is None:
                raise AccountNotFoundError(account_id)
            revision = self._repository.get_revision(account_id, account.current_revision, conn=conn)
            if revision is None:
                raise CredentialResolutionError(
                    f"账号 {account_id} 缺少最新修订 {account.current_revision}"
                )
            config = revision["config"] or {}
            credentials = self._decrypt_credentials(revision, account_id)
            errors = self._validate_credentials(account.provider_kind, config, credentials)
            if errors:
                result = AccountTestResult(ok=False, message="配置校验失败", errors=errors)
            else:
                test_fn = tester or self._default_tester or self._noop_tester
                result = test_fn(credentials, config)
            status = TEST_STATUS_OK if result.ok else TEST_STATUS_FAILED
            now = utc_now()
            hint = str(result.identity_hint or "").strip() or account.identity_hint
            self._repository.update_account_test(account_id, status, result.message, now, conn=conn)
            self._repository.update_identity_hint(account_id, hint, conn=conn)
        return result

    # ---- activation / disabling / deletion ----

    def activate_account(
        self,
        scope: str,
        account_id: str,
        expected_selection_revision: int | None,
    ) -> AccountSelection:
        scope = str(scope or "").strip() or DEFAULT_SCOPE
        with self._database.connect() as conn:
            account = self._repository.get_account(account_id, conn=conn)
            if account is None:
                raise AccountNotFoundError(account_id)
            if not account.enabled:
                raise AccountDisabledError("被禁用的账号不能设为当前账号")
            if account.last_test_status != TEST_STATUS_OK:
                raise AccountNotTestedError("检测未通过的账号不能设为当前账号")
            return self._repository.upsert_selection(
                account.provider_kind,
                scope,
                account_id,
                account.current_revision,
                expected_selection_revision,
                conn=conn,
            )

    def disable_account(self, account_id: str) -> None:
        with self._database.connect() as conn:
            account = self._repository.get_account(account_id, conn=conn)
            if account is None:
                raise AccountNotFoundError(account_id)
            if self._repository.selection_references_account(account_id, conn=conn):
                raise ActiveAccountDisableError("当前账号不能直接禁用，请先切换到其他账号")
            self._repository.set_account_enabled(account_id, False, conn=conn)

    def delete_account(self, account_id: str) -> None:
        with self._database.connect() as conn:
            account = self._repository.get_account(account_id, conn=conn)
            if account is None:
                raise AccountNotFoundError(account_id)
            if self._repository.selection_references_account(account_id, conn=conn):
                raise AccountInUseError("当前账号不能删除，请先切换到其他账号")
            if self._repository.count_non_terminal_jobs_referencing_account(
                account_id, conn=conn
            ) > 0:
                raise AccountInUseError("账号仍被进行中的任务引用，无法删除")
            self._repository.delete_account(account_id, conn=conn)

    # ---- resolution / queries ----

    def get_active_account(self, provider_kind: str) -> AccountSelection | None:
        return self._repository.get_selection(str(provider_kind or "").strip(), DEFAULT_SCOPE)

    def list_active_selections(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for selection in self._repository.list_selections():
            result[selection.provider_kind] = self._serialize_selection(selection)
        return result

    def resolve_credentials(self, account_id: str, revision: int) -> dict[str, Any]:
        """解密指定修订的凭证。任务冻结 account_id + revision，旧修订仍可解析。"""
        with self._database.connect() as conn:
            rev = self._repository.get_revision(account_id, revision, conn=conn)
        if rev is None:
            raise CredentialResolutionError(f"账号 {account_id} 修订 {revision} 不存在")
        return self._decrypt_credentials(rev, account_id)

    def _decrypt_credentials(self, revision: dict[str, Any], account_id: str) -> dict[str, Any]:
        try:
            plaintext = decrypt(
                self._master_key(),
                revision["credential_key_id"],
                revision["credentials_ciphertext"],
            )
        except MasterKeyError as exc:
            raise CredentialResolutionError(
                f"账号 {account_id} 凭证解密失败：{exc}"
            ) from exc
        try:
            data = json.loads(plaintext.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialResolutionError(f"账号 {account_id} 凭证格式损坏") from exc
        if not isinstance(data, dict):
            raise CredentialResolutionError(f"账号 {account_id} 凭证不是 JSON 对象")
        return data

    # ---- serialization (API 安全视图，绝不包含凭证) ----

    @staticmethod
    def serialize_account(account: ProviderAccount) -> dict[str, Any]:
        return {
            "account_id": account.account_id,
            "provider_kind": account.provider_kind,
            "name": account.name,
            "enabled": account.enabled,
            "current_revision": account.current_revision,
            "identity_hint": account.identity_hint,
            "configured": True,
            "last_test_status": account.last_test_status,
            "last_test_message": account.last_test_message,
            "last_tested_at": account.last_tested_at,
            "created_at": account.created_at,
            "updated_at": account.updated_at,
        }


__all__ = [
    "ACCOUNT_ID_PREFIX",
    "AccountDisabledError",
    "AccountNotTestedError",
    "AccountTester",
    "ActiveAccountDisableError",
    "CredentialResolutionError",
    "DEFAULT_SCOPE",
    "ImmutableFieldError",
    "InvalidConfigError",
    "MasterKeyProvider",
    "PROVIDER_CATALOG",
    "ProviderAccountService",
    "ProviderMismatchError",
    "TEST_STATUS_FAILED",
    "TEST_STATUS_OK",
    "TEST_STATUS_UNTESTED",
    "provider_catalog",
]
