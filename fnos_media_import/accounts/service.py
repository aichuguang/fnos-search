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
from .crypto import PURPOSE_PROVIDER_ACCOUNT, MasterKeyError, decrypt, encrypt
from .repository import (
    AccountConflictError,
    AccountInUseError,
    AccountNotFoundError,
    ProviderAccountError,
    ProviderAccountRepository,
)
from ..providers.manifests import BUILTIN_MANIFESTS
from ..providers.base import manifest_dict

TEST_STATUS_UNTESTED = "untested"
TEST_STATUS_OK = "ok"
TEST_STATUS_FAILED = "failed"

DEFAULT_SCOPE = "default"
ACCOUNT_ID_PREFIX = "acc_"

MasterKeyProvider = Callable[[], bytes]
AccountTester = Callable[[str, dict[str, Any], dict[str, Any]], AccountTestResult]


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


def _manifest_catalog_item(item: Any) -> dict[str, Any]:
    """Return one canonical, JSON-safe Provider manifest for API and forms.

    The previous implementation projected a manifest into a second ``schema`` /
    ``locator_schema`` shape.  That made plugin providers lose fields as soon as
    they were passed through a different composition root.  V1 keeps the
    manifest names intact; callers may still use a deep copy safely.
    """

    if hasattr(item, "provider_kind"):
        value = manifest_dict(item)
    else:
        value = dict(item or {})
    value.setdefault("account_schema", {"fields": []})
    value.setdefault("native_locator_schema", None)
    value.setdefault("runtime_schema", None)
    value.setdefault("auth_methods", [])
    value.setdefault("search_sources", [])
    value.setdefault("components", [])
    value.setdefault("capabilities", {})
    value.setdefault("icon_text", "")
    # Read-only aliases keep existing consumers from crashing while the
    # manifest itself remains the sole configuration source.  New code must
    # use account_schema/native_locator_schema; these aliases are never read
    # when validating or persisting credentials.
    value.setdefault("schema", copy.deepcopy(value["account_schema"]))
    value.setdefault("locator_schema", copy.deepcopy(value["native_locator_schema"]))
    return copy.deepcopy(value)


PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    item.provider_kind: _manifest_catalog_item(item)
    for item in BUILTIN_MANIFESTS.values()
}


def provider_catalog() -> list[dict[str, Any]]:
    """返回深拷贝的 Provider 目录，避免调用方改动模块常量。"""
    return [copy.deepcopy(PROVIDER_CATALOG[kind]) for kind in sorted(PROVIDER_CATALOG)]


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
        catalog_provider: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._database = database
        self._repository = ProviderAccountRepository(database)
        self._master_key_provider = master_key_provider
        self._default_tester = tester
        self._catalog_provider = catalog_provider

    # ---- master key ----

    def _master_key(self) -> bytes:
        return self._master_key_provider()

    @staticmethod
    def _new_account_id() -> str:
        return f"{ACCOUNT_ID_PREFIX}{secrets.token_hex(8)}"

    # ---- catalog ----

    def catalog(self) -> list[dict[str, Any]]:
        if self._catalog_provider is None:
            return provider_catalog()
        return [
            _manifest_catalog_item(item)
            for item in self._catalog_provider()
        ]

    def set_catalog_provider(
        self,
        provider: Callable[[], list[dict[str, Any]]],
    ) -> None:
        """Bind the runtime Provider registry as the sole manifest catalog."""

        self._catalog_provider = provider

    def set_tester(self, tester: AccountTester) -> None:
        """Bind the registry-backed account probe used by all future tests."""

        self._default_tester = tester

    def _catalog_by_kind(self) -> dict[str, dict[str, Any]]:
        return {str(item["provider_kind"]): item for item in self.catalog()}

    def _validate_credentials(
        self, provider_kind: str, config: dict[str, Any], credentials: dict[str, Any]
    ) -> list[str]:
        errors: list[str] = []
        catalog = self._catalog_by_kind()
        if provider_kind not in catalog:
            return [f"Provider 未注册：{provider_kind}"]
        fields = (catalog[provider_kind].get("account_schema") or {}).get("fields") or []
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
        if kind not in self._catalog_by_kind():
            raise InvalidConfigError(f"不支持的 Provider：{kind}")
        errors = self._validate_credentials(kind, config or {}, credentials or {})
        if errors:
            raise InvalidConfigError("；".join(errors))
        master_key = self._master_key()
        ciphertext, key_id = encrypt(
            master_key,
            json.dumps(credentials or {}, ensure_ascii=False, sort_keys=True),
            purpose=PURPOSE_PROVIDER_ACCOUNT,
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

    def prepare_authenticated_account(
        self,
        provider_kind: str,
        account_data: dict[str, Any],
        *,
        tester: AccountTester | None = None,
        cache_namespace: str = "",
    ) -> tuple[dict[str, Any], AccountTestResult]:
        """Validate a Provider-login result without publishing account state.

        Provider authorization can return a one-time credential.  The auth
        session service freezes that result before calling this method, then
        publishes the prepared account and the session terminal state in one
        SQLite transaction via :meth:`commit_authenticated_account`.

        Keeping the network probe outside that transaction avoids holding a
        database write lock while still making the eventual account mutation
        atomic with the auth-session CAS.
        """

        kind = str(provider_kind or "").strip()
        if kind not in self._catalog_by_kind():
            raise InvalidConfigError(f"不支持的 Provider：{kind}")

        data = dict(account_data or {})
        existing_id = str(data.get("account_id") or "").strip()
        name = str(data.get("name") or "").strip() or "主账号"
        supplied_config = dict(data.get("config") or {})
        supplied_credentials = dict(data.get("credentials") or {})
        expected_revision: int | None = None
        previous_identity = ""

        if existing_id:
            with self._database.connect() as conn:
                account = self._repository.get_account(existing_id, conn=conn)
                if account is None or account.provider_kind != kind:
                    raise ProviderMismatchError("重新登录的账号不存在或 Provider 不匹配")
                if not account.enabled:
                    raise AccountDisabledError("被禁用的账号不能通过登录会话重新激活")
                expected_revision = account.current_revision
                revision = self._repository.get_revision(
                    existing_id, expected_revision, conn=conn
                )
                if revision is None:
                    raise CredentialResolutionError(
                        f"账号 {existing_id} 缺少最新修订 {expected_revision}"
                    )
                merged_config = dict(revision.get("config") or {})
                merged_config.update(supplied_config)
                merged_credentials = self._decrypt_credentials(revision, existing_id)
                for key, value in supplied_credentials.items():
                    if value is None:
                        merged_credentials.pop(key, None)
                    elif str(value).strip():
                        merged_credentials[key] = value
                previous_identity = account.identity_hint
        else:
            merged_config = supplied_config
            merged_credentials = supplied_credentials

        result = self._run_account_test(
            kind,
            merged_credentials,
            merged_config,
            tester=tester,
            cache_namespace=(
                str(cache_namespace or "").strip()
                or f"auth:{existing_id or kind}"
            ),
        )
        hint = (
            str(result.identity_hint or "").strip()
            or str(data.get("identity_hint") or "").strip()
            or self._derive_identity_hint(kind, merged_config, merged_credentials)
            or previous_identity
        )
        prepared: dict[str, Any] = {
            "provider_kind": kind,
            "account_id": existing_id or None,
            "expected_revision": expected_revision,
            "name": name,
            "config": merged_config,
            "identity_hint": hint,
            "test_message": str(result.message or ""),
        }
        if result.ok:
            ciphertext, key_id = encrypt(
                self._master_key(),
                json.dumps(merged_credentials, ensure_ascii=False, sort_keys=True),
                purpose=PURPOSE_PROVIDER_ACCOUNT,
            )
            prepared["credentials_ciphertext"] = ciphertext
            prepared["credential_key_id"] = key_id
        return prepared, result

    def commit_authenticated_account(
        self,
        prepared: dict[str, Any],
        *,
        conn: Any,
    ) -> ProviderAccount:
        """Publish a successfully probed login result using the caller transaction.

        The caller must first acquire its durable auth-session CAS guard on the
        same connection.  If that guard is stale, no account row is written; if
        any later session update fails, the caller rolls this whole transaction
        back.  This removes the former account-created/session-not-completed
        crash window.
        """

        kind = str(prepared.get("provider_kind") or "").strip()
        existing_id = str(prepared.get("account_id") or "").strip()
        name = str(prepared.get("name") or "").strip() or "主账号"
        config = dict(prepared.get("config") or {})
        ciphertext = prepared.get("credentials_ciphertext")
        key_id = str(prepared.get("credential_key_id") or "").strip()
        if not isinstance(ciphertext, (bytes, bytearray)) or not key_id:
            raise InvalidConfigError("账号登录结果尚未通过检测，禁止落库")

        now = utc_now()
        hint = str(prepared.get("identity_hint") or "").strip()
        test_message = str(prepared.get("test_message") or "")

        if existing_id:
            account = self._repository.get_account(existing_id, conn=conn)
            expected_revision = int(prepared.get("expected_revision") or 0)
            if account is None or account.provider_kind != kind:
                raise ProviderMismatchError("重新登录的账号不存在或 Provider 不匹配")
            if account.current_revision != expected_revision:
                raise AccountConflictError("账号凭证已被其他操作更新，请刷新后重试")
            next_revision = expected_revision + 1
            self._repository.insert_revision(
                existing_id,
                next_revision,
                json.dumps(config, ensure_ascii=False),
                bytes(ciphertext),
                key_id,
                conn=conn,
            )
            self._repository.set_account_revision(existing_id, next_revision, conn=conn)
            self._repository.update_account_test(
                existing_id, TEST_STATUS_OK, test_message, now, conn=conn
            )
            if hint:
                self._repository.update_identity_hint(existing_id, hint, conn=conn)
            if name:
                self._repository.update_account_name(existing_id, name, conn=conn)
            self._advance_account_references(conn, existing_id, next_revision)
            account_id = existing_id
            account_revision = next_revision
        else:
            account_id = self._new_account_id()
            account_revision = 1
            account = ProviderAccount(
                account_id=account_id,
                provider_kind=kind,
                name=name,
                enabled=True,
                current_revision=account_revision,
                identity_hint=hint,
                last_test_status=TEST_STATUS_OK,
                last_test_message=test_message,
                last_tested_at=now,
                created_at=now,
                updated_at=now,
            )
            self._repository.create_account(account, conn=conn)
            self._repository.insert_revision(
                account_id,
                account_revision,
                json.dumps(config, ensure_ascii=False),
                bytes(ciphertext),
                key_id,
                conn=conn,
            )

        selection = self._repository.get_selection(kind, DEFAULT_SCOPE, conn=conn)
        if (
            selection is None
            or selection.account_id != account_id
            or selection.account_revision != account_revision
        ):
            self._repository.upsert_selection(
                kind,
                DEFAULT_SCOPE,
                account_id,
                account_revision,
                selection.selection_revision if selection else None,
                conn=conn,
            )
        committed = self._repository.get_account(account_id, conn=conn)
        if committed is None:
            raise AccountNotFoundError(account_id)
        return committed

    def get_account(self, account_id: str) -> ProviderAccount | None:
        return self._repository.get_account(account_id)

    def list_accounts(self, provider_kind: str | None = None) -> list[ProviderAccount]:
        return self._repository.list_accounts(provider_kind)

    def update_account_name(self, account_id: str, name: str) -> ProviderAccount:
        normalized = str(name or "").strip()
        if not normalized:
            raise InvalidConfigError("账号名称不能为空")
        if len(normalized) > 80:
            raise InvalidConfigError("账号名称不能超过 80 个字符")
        with self._database.connect() as conn:
            account = self._repository.get_account(account_id, conn=conn)
            if account is None:
                raise AccountNotFoundError(account_id)
            self._repository.update_account_name(account_id, normalized, conn=conn)
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
            current = self._repository.get_revision(
                account_id, account.current_revision, conn=conn
            )
            if current is None:
                raise CredentialResolutionError(
                    f"账号 {account_id} 缺少最新修订 {account.current_revision}"
                )
            merged_config = dict(current.get("config") or {})
            merged_config.update(dict(config or {}))
            merged_credentials = self._decrypt_credentials(current, account_id)
            for key, value in dict(credentials or {}).items():
                # 管理页编辑时密码留空表示保留旧值；显式 null 才表示删除可选字段。
                if value is None:
                    merged_credentials.pop(key, None)
                elif str(value).strip():
                    merged_credentials[key] = value
            errors = self._validate_credentials(
                account.provider_kind, merged_config, merged_credentials
            )
            if errors:
                raise InvalidConfigError("；".join(errors))
            ciphertext, key_id = encrypt(
                self._master_key(),
                json.dumps(merged_credentials, ensure_ascii=False, sort_keys=True),
                purpose=PURPOSE_PROVIDER_ACCOUNT,
            )
            new_revision = account.current_revision + 1
            self._repository.insert_revision(
                account_id,
                new_revision,
                json.dumps(merged_config, ensure_ascii=False),
                ciphertext,
                key_id,
                conn=conn,
            )
            self._repository.set_account_revision(account_id, new_revision, conn=conn)
            self._repository.update_account_test(
                account_id, TEST_STATUS_UNTESTED, "", None, conn=conn
            )
            hint = str(identity_hint or "").strip() or self._derive_identity_hint(
                account.provider_kind, merged_config, merged_credentials
            )
            if hint:
                self._repository.update_identity_hint(account_id, hint, conn=conn)
            updated = self._repository.get_account(account_id, conn=conn)
        return updated  # type: ignore[return-value]

    def revise_credentials_if_healthy(
        self,
        account_id: str,
        *,
        config: dict[str, Any],
        credentials: dict[str, Any],
        identity_hint: str = "",
        name: str = "",
        tester: AccountTester | None = None,
    ) -> tuple[ProviderAccount, AccountTestResult]:
        """Validate a re-login candidate before publishing a new revision.

        A failed re-login must leave the previous revision, health result, name,
        and active selection untouched. Network validation therefore happens
        before the short database transaction that publishes the new revision.
        """
        with self._database.connect() as conn:
            account = self._repository.get_account(account_id, conn=conn)
            if account is None:
                raise AccountNotFoundError(account_id)
            expected_revision = account.current_revision
            current = self._repository.get_revision(account_id, expected_revision, conn=conn)
            if current is None:
                raise CredentialResolutionError(
                    f"账号 {account_id} 缺少最新修订 {expected_revision}"
                )
            merged_config = dict(current.get("config") or {})
            merged_config.update(dict(config or {}))
            merged_credentials = self._decrypt_credentials(current, account_id)
            for key, value in dict(credentials or {}).items():
                if value is None:
                    merged_credentials.pop(key, None)
                elif str(value).strip():
                    merged_credentials[key] = value

        result = self._run_account_test(
            account.provider_kind,
            merged_credentials,
            merged_config,
            tester=tester,
            cache_namespace=f"account:{account_id}",
        )
        if not result.ok:
            return account, result

        ciphertext, key_id = encrypt(
            self._master_key(),
            json.dumps(merged_credentials, ensure_ascii=False, sort_keys=True),
            purpose=PURPOSE_PROVIDER_ACCOUNT,
        )
        next_revision = expected_revision + 1
        hint = (
            str(result.identity_hint or "").strip()
            or str(identity_hint or "").strip()
            or self._derive_identity_hint(
                account.provider_kind, merged_config, merged_credentials
            )
            or account.identity_hint
        )
        with self._database.connect() as conn:
            latest = self._repository.get_account(account_id, conn=conn)
            if latest is None:
                raise AccountNotFoundError(account_id)
            if latest.current_revision != expected_revision:
                raise AccountConflictError("账号凭证已被其他操作更新，请刷新后重试")
            self._repository.insert_revision(
                account_id,
                next_revision,
                json.dumps(merged_config, ensure_ascii=False),
                ciphertext,
                key_id,
                conn=conn,
            )
            self._repository.set_account_revision(account_id, next_revision, conn=conn)
            self._repository.update_account_test(
                account_id,
                TEST_STATUS_OK,
                result.message,
                utc_now(),
                conn=conn,
            )
            self._repository.update_identity_hint(account_id, hint, conn=conn)
            self._advance_account_references(conn, account_id, next_revision)
            if str(name or "").strip():
                self._repository.update_account_name(account_id, str(name).strip(), conn=conn)
            updated = self._repository.get_account(account_id, conn=conn)
        return updated, result  # type: ignore[return-value]

    def refresh_runtime_credentials(
        self,
        account_id: str,
        credentials: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> ProviderAccount:
        """Persist Provider-issued token refresh and advance active selections.

        This path is only called by a trusted Adapter. It preserves the latest
        successful health status and never accepts account identity changes.
        """
        patch = {key: value for key, value in dict(credentials or {}).items() if value not in (None, "")}
        expected = None
        if expected_revision is not None:
            try:
                expected = int(expected_revision)
            except (TypeError, ValueError) as exc:
                raise AccountConflictError("运行时凭证修订无效") from exc
            if expected <= 0:
                raise AccountConflictError("运行时凭证修订无效")
        with self._database.connect() as conn:
            account = self._repository.get_account(account_id, conn=conn)
            if account is None:
                raise AccountNotFoundError(account_id)
            if expected is not None and account.current_revision != expected:
                raise AccountConflictError("账号凭证修订已变化，拒绝旧任务覆盖当前凭证")
            if not patch:
                return account
            current = self._repository.get_revision(
                account_id, account.current_revision, conn=conn
            )
            if current is None:
                raise CredentialResolutionError(
                    f"账号 {account_id} 缺少最新修订 {account.current_revision}"
                )
            config = dict(current.get("config") or {})
            existing_credentials = self._decrypt_credentials(current, account_id)
            merged = dict(existing_credentials)
            merged.update(patch)
            errors = self._validate_credentials(account.provider_kind, config, merged)
            if errors:
                raise InvalidConfigError("；".join(errors))
            if merged == existing_credentials:
                return account
            ciphertext, key_id = encrypt(
                self._master_key(),
                json.dumps(merged, ensure_ascii=False, sort_keys=True),
                purpose=PURPOSE_PROVIDER_ACCOUNT,
            )
            new_revision = account.current_revision + 1
            self._repository.insert_revision(
                account_id,
                new_revision,
                json.dumps(config, ensure_ascii=False),
                ciphertext,
                key_id,
                conn=conn,
            )
            self._repository.set_account_revision(account_id, new_revision, conn=conn)
            self._advance_account_references(conn, account_id, new_revision)
            updated = self._repository.get_account(account_id, conn=conn)
        return updated  # type: ignore[return-value]

    def _advance_account_references(
        self,
        conn: Any,
        account_id: str,
        account_revision: int,
    ) -> None:
        """Move only future work to a healthy credential revision.

        Existing plans retain their frozen selection/binding revisions. Active
        selections and target bindings advance so newly-created plans do not
        continue using a stale token after refresh or re-login.
        """
        selections = [
            selection
            for selection in self._repository.list_selections(conn=conn)
            if selection.account_id == account_id
        ]
        for selection in selections:
            if selection.account_revision == account_revision:
                continue
            self._repository.upsert_selection(
                selection.provider_kind,
                selection.scope,
                account_id,
                account_revision,
                selection.selection_revision,
                conn=conn,
            )

        bindings = conn.execute(
            "SELECT b.binding_id, b.current_revision, r.locator_json, r.account_revision "
            "FROM storage_target_account_bindings b "
            "JOIN storage_target_binding_revisions r "
            "ON r.binding_id=b.binding_id AND r.revision=b.current_revision "
            "WHERE b.account_id=? AND b.enabled=1",
            (account_id,),
        ).fetchall()
        now = utc_now()
        for binding in bindings:
            if int(binding["account_revision"] or 0) == account_revision:
                continue
            next_revision = int(binding["current_revision"]) + 1
            conn.execute(
                "INSERT INTO storage_target_binding_revisions "
                "(binding_id, revision, locator_json, account_revision, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    binding["binding_id"],
                    next_revision,
                    binding["locator_json"],
                    account_revision,
                    now,
                ),
            )
            conn.execute(
                "UPDATE storage_target_account_bindings "
                "SET current_revision=?, updated_at=? WHERE binding_id=?",
                (next_revision, now, binding["binding_id"]),
            )

    # ---- testing ----

    def test_account(
        self,
        account_id: str,
        *,
        tester: AccountTester | None = None,
        persist_result: bool = True,
    ) -> AccountTestResult:
        """Run the real account probe and optionally persist its health result.

        ``persist_result=False`` keeps preflight CAS-checked but state read-only:
        it does not update account health, identity, selections, or bindings.
        """
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
            tested_revision = account.current_revision
            provider_kind = account.provider_kind

        result = self._run_account_test(
            provider_kind,
            credentials,
            config,
            tester=tester,
            cache_namespace=f"account:{account_id}",
        )
        with self._database.connect() as conn:
            current = self._repository.get_account(account_id, conn=conn)
            if current is None:
                raise AccountNotFoundError(account_id)
            if current.current_revision != tested_revision:
                raise AccountConflictError(
                    "账号凭证在检测期间已更新，本次检测结果已丢弃，请重新检测"
                )
            if not persist_result:
                return result
            status = TEST_STATUS_OK if result.ok else TEST_STATUS_FAILED
            now = utc_now()
            hint = str(result.identity_hint or "").strip() or current.identity_hint
            self._repository.update_account_test(account_id, status, result.message, now, conn=conn)
            self._repository.update_identity_hint(account_id, hint, conn=conn)
            if result.ok:
                self._advance_account_references(conn, account_id, tested_revision)
        return result

    def _run_account_test(
        self,
        provider_kind: str,
        credentials: dict[str, Any],
        config: dict[str, Any],
        *,
        tester: AccountTester | None = None,
        cache_namespace: str = "",
    ) -> AccountTestResult:
        errors = self._validate_credentials(provider_kind, config, credentials)
        if errors:
            return AccountTestResult(ok=False, message="配置校验失败", errors=errors)
        test_fn = tester or self._default_tester
        if test_fn is None:
            raise InvalidConfigError(
                f"{provider_kind} 未注入真实账号检测器，禁止产生假健康状态"
            )
        probe_config = dict(config or {})
        if provider_kind == "tianyi" and cache_namespace:
            probe_config["_cache_namespace"] = cache_namespace
        return test_fn(provider_kind, credentials, probe_config)

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
            if self._repository.count_frozen_plans_referencing_account(
                account_id, conn=conn
            ) > 0:
                raise AccountInUseError("账号仍被历史或进行中的执行计划引用，无法删除；请禁用账号")
            self._repository.delete_account(account_id, conn=conn)

    # ---- resolution / queries ----

    def get_active_account(self, provider_kind: str) -> AccountSelection | None:
        return self._repository.get_selection(str(provider_kind or "").strip(), DEFAULT_SCOPE)

    def get_selection(self, provider_kind: str, scope: str = DEFAULT_SCOPE) -> AccountSelection | None:
        return self._repository.get_selection(
            str(provider_kind or "").strip(), str(scope or DEFAULT_SCOPE).strip()
        )

    def get_selection_revision(
        self,
        provider_kind: str,
        scope: str,
        selection_revision: int,
    ) -> AccountSelection | None:
        """Return a frozen historical account selection for plan validation."""
        return self._repository.get_selection_revision(
            str(provider_kind or "").strip(),
            str(scope or DEFAULT_SCOPE).strip(),
            int(selection_revision),
        )

    def has_revision(self, account_id: str, revision: int) -> bool:
        return self._repository.get_revision(account_id, int(revision)) is not None

    def list_active_selections(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for selection in self._repository.list_selections():
            result[f"{selection.provider_kind}:{selection.scope}"] = self._serialize_selection(selection)
        return result

    def resolve_credentials(self, account_id: str, revision: int) -> dict[str, Any]:
        """解密指定修订的凭证。任务冻结 account_id + revision，旧修订仍可解析。"""
        with self._database.connect() as conn:
            rev = self._repository.get_revision(account_id, revision, conn=conn)
        if rev is None:
            raise CredentialResolutionError(f"账号 {account_id} 修订 {revision} 不存在")
        return self._decrypt_credentials(rev, account_id)

    def resolve_account_context(self, account_id: str, revision: int) -> dict[str, Any]:
        with self._database.connect() as conn:
            account = self._repository.get_account(account_id, conn=conn)
            rev = self._repository.get_revision(account_id, revision, conn=conn)
        if account is None or rev is None:
            raise CredentialResolutionError(f"账号修订不存在：{account_id}@{revision}")
        return {
            "account_id": account_id,
            "provider_kind": account.provider_kind,
            "revision": int(revision),
            "config": dict(rev.get("config") or {}),
            "credentials": self._decrypt_credentials(rev, account_id),
        }

    def safe_account_config(self, account_id: str, revision: int | None = None) -> dict[str, Any]:
        account = self.get_account(account_id)
        if account is None:
            raise AccountNotFoundError(account_id)
        selected_revision = int(revision or account.current_revision)
        raw = self._repository.get_revision(account_id, selected_revision)
        if raw is None:
            raise CredentialResolutionError(f"账号修订不存在：{account_id}@{selected_revision}")
        return dict(raw.get("config") or {})

    def _decrypt_credentials(self, revision: dict[str, Any], account_id: str) -> dict[str, Any]:
        try:
            plaintext = decrypt(
                self._master_key(),
                revision["credential_key_id"],
                revision["credentials_ciphertext"],
                purpose=PURPOSE_PROVIDER_ACCOUNT,
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
