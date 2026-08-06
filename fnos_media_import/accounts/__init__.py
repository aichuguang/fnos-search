"""统一账号体系：Provider 账号、不可变凭证修订、激活选择与凭证加密。

对应 docs/V1.0多链路设计文档.md §5 与 §16.1。对外提供：

- ``crypto``：AES-256-GCM 信封加密，主密钥来自 FNOS_ACCOUNT_MASTER_KEY。
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
    UnknownKeyIdError,
    decrypt,
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
    "ProviderAccountError",
    "ProviderAccountRepository",
    "ProviderAccountService",
    "ProviderMismatchError",
    "SelectionChangedError",
    "UnknownKeyIdError",
    "accounts_blueprint_for_service",
    "create_accounts_blueprint",
    "decrypt",
    "encrypt",
    "master_key_from_env",
    "provider_catalog",
]
