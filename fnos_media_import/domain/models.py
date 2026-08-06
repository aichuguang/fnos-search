"""V1.0 统一账号与多链路入库的领域类型。

这些类型是各模块的共享契约，按 docs/V1.0多链路设计文档.md 定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

EXECUTION_PLAN_VERSION = 4

ProviderKind = str
TransferStrategy = Literal[
    "native_save",
    "native_offline",
    "rclone_copy",
    "relay_upload_mobile",
]
Audience = Literal["admin", "guest"]
TargetKind = Literal["provider", "local", "openlist_generic"]


@dataclass(frozen=True)
class SourceDescriptor:
    """服务端从可信资源记录解析出的来源描述。"""

    provider_kind: str
    source_form: str  # share | magnet | local | http
    canonical_source_id: str
    account_scope: str = "default"
    selected_items_hash: str = ""


@dataclass(frozen=True)
class DestinationDescriptor:
    destination_id: str
    provider_kind: str
    account_scope: str | None = None
    target_revision: int = 1


@dataclass(frozen=True)
class ProviderAccount:
    account_id: str
    provider_kind: str
    name: str
    enabled: bool
    current_revision: int
    identity_hint: str
    last_test_status: str
    last_test_message: str
    last_tested_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AccountSelection:
    provider_kind: str
    scope: str
    account_id: str
    account_revision: int
    selection_revision: int


@dataclass(frozen=True)
class OpenListConnection:
    connection_id: str
    name: str
    base_url: str
    username: str
    credential_configured: bool
    enabled: bool
    revision: int
    last_test_status: str
    last_tested_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class OpenListMount:
    mount_id: str
    connection_id: str
    name: str
    provider_kind: str | None
    account_id: str | None
    webdav_root: str
    namespace_id: str
    readable: bool
    writable: bool
    enabled: bool
    revision: int
    identity_status: str  # verified | declared | mismatch | unverified


@dataclass(frozen=True)
class NativeLocator:
    """同源原生保存 / 移动云上传时由 Provider Adapter 解释的定位器。"""

    kind: str  # folder_id | provider_path | none
    value: str = ""


@dataclass(frozen=True)
class RcloneLocator:
    """跨盘写入时由 rclone 解释的定位器。"""

    connection_id: str
    mount_id: str
    relative_path: str


@dataclass(frozen=True)
class LocalLocator:
    root: str  # 受控容器根，如 /storage-targets/<target-id>
    relative_path: str = ""


@dataclass(frozen=True)
class OpenListLocator:
    connection_id: str
    mount_id: str
    path: str


@dataclass(frozen=True)
class TargetCategory:
    """单个分类的三套定位器 + 面向访客的标签。"""

    category_key: str
    public_label: str
    native_locator: NativeLocator | None = None
    rclone_locator: RcloneLocator | LocalLocator | None = None
    organizer_locator: OpenListLocator | None = None


@dataclass(frozen=True)
class StorageTarget:
    destination_id: str
    name: str
    provider_kind: str
    account_scope: str | None
    target_kind: TargetKind
    enabled: bool
    audience: Audience
    current_revision: int
    categories: dict[str, TargetCategory] = field(default_factory=dict)

    def category(self, category_key: str) -> TargetCategory | None:
        return self.categories.get(category_key)


@dataclass(frozen=True)
class SourceDestinationRule:
    rule_id: str
    source_provider_kind: str
    source_form: str
    category_key: str
    destination_id: str
    guest_allowed: bool
    admin_allowed: bool
    is_default: bool
    priority: int
    enabled: bool


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_search: bool = False
    supports_share_inspection: bool = False
    supports_native_save: bool = False
    supports_native_offline: bool = False
    supports_operation_polling: bool = False
    supports_native_verify: bool = False
    supports_relay_upload: bool = False


@dataclass(frozen=True)
class AccountTestResult:
    ok: bool
    identity_hint: str = ""
    message: str = ""
    errors: list[str] = field(default_factory=list)
