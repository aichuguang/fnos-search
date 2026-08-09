"""统一账号体系：Provider 账号、不可变凭证修订、激活选择与凭证加密。

对应 docs/V1.0多链路设计文档.md §5 与 §16.1。对外提供：

- ``crypto``：从 FNOS_ROOT_KEY 按用途派生子密钥并执行 AES-256-GCM 信封加密。
- ``ProviderAccountRepository``：三类账号表的 CRUD 与任务引用计数。
- ``ProviderAccountService``：账号生命周期、激活事务与凭证解析。
- ``create_accounts_blueprint`` / ``accounts_blueprint_for_service``：管理 API。
"""

from __future__ import annotations

from .blueprint import (
    AccountsRouteContext,
    accounts_blueprint_for_service,
    create_accounts_blueprint,
)
from .crypto import (
    DEFAULT_KEY_ID,
    DecryptionError,
    MasterKeyError,
    MasterKeyMissingError,
    PURPOSE_APP_SESSION,
    PURPOSE_AUTH_SESSION,
    PURPOSE_NOTIFICATION,
    PURPOSE_OPENLIST,
    PURPOSE_PROVIDER_ACCOUNT,
    PURPOSE_SOURCE_RECORD,
    ROOT_KEY_ENV,
    UnknownKeyIdError,
    decrypt,
    derive_key,
    encrypt,
    master_key_from_env,
)
from .repository import (
    AccountConflictError,
    AccountInUseError,
    AccountNotFoundError,
    ProviderAccountError,
    ProviderAccountRepository,
    SelectionChangedError,
)
from .service import (
    AccountDisabledError,
    AccountNotTestedError,
    ActiveAccountDisableError,
    CredentialResolutionError,
    ImmutableFieldError,
    InvalidConfigError,
    ProviderAccountService,
    ProviderMismatchError,
    provider_catalog,
)

__all__ = [
    "AccountConflictError",
    "AccountDisabledError",
    "AccountInUseError",
    "AccountNotTestedError",
    "AccountNotFoundError",
    "AccountsRouteContext",
    "ActiveAccountDisableError",
    "CredentialResolutionError",
    "DEFAULT_KEY_ID",
    "DecryptionError",
    "ImmutableFieldError",
    "InvalidConfigError",
    "MasterKeyError",
    "MasterKeyMissingError",
    "PURPOSE_APP_SESSION",
    "PURPOSE_AUTH_SESSION",
    "PURPOSE_NOTIFICATION",
    "PURPOSE_OPENLIST",
    "PURPOSE_PROVIDER_ACCOUNT",
    "PURPOSE_SOURCE_RECORD",
    "ProviderAccountError",
    "ProviderAccountRepository",
    "ProviderAccountService",
    "ProviderMismatchError",
    "SelectionChangedError",
    "UnknownKeyIdError",
    "ROOT_KEY_ENV",
    "accounts_blueprint_for_service",
    "create_accounts_blueprint",
    "decrypt",
    "derive_key",
    "encrypt",
    "master_key_from_env",
    "provider_catalog",
]
