"""V1.0 统一账号与多链路入库的领域类型。

这些类型是各模块的共享契约，按 docs/V1.0多链路设计文档.md 定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from typing import Any, Literal, Mapping

EXECUTION_PLAN_VERSION = 1

ProviderKind = str
TransferStrategy = Literal[
    "native_save",
    "native_offline",
    "rclone_copy",
    "relay_upload_provider",
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
    search_source_id: str = ""
    source_record_id: str = ""
    source_record_revision: int = 1
    expected_file_count: int = 0
    expected_total_bytes: int = 0
    # The complete, provider-neutral selection frozen with the source.  The
    # legacy ``selected_items_hash`` field remains as a denormalised alias so
    # callers that only understand the original descriptor continue to work.
    selection: "SelectionSnapshot | None" = None

    @property
    def selection_snapshot(self) -> "SelectionSnapshot | None":
        return self.selection


@dataclass(frozen=True)
class SelectionSnapshot:
    """Canonical selection used by planning, idempotency and Provider calls.

    Providers expose different selection shapes (for example Quark's
    ``quark_selection`` and SixPan's ``ignore_files``).  Keeping only a hash
    or only ``selected_items`` loses information and permits two requests to
    receive the same idempotency key while asking the Provider for different
    content.  This value object stores the normalized common fields together
    with the provider-specific payload and derives one deterministic hash.

    ``dict`` is deliberately used for ``provider_selection`` rather than a
    Provider-specific class: adding a new Provider must not require changing
    the planner's core model.  The object is treated as immutable by callers;
    ``__post_init__`` deep-normalizes input and copies all containers.
    """

    provider_kind: str
    selected_items: tuple[Any, ...] = ()
    ignore_files: tuple[str, ...] = ()
    provider_selection: dict[str, Any] = field(default_factory=dict)
    selection_hash: str = ""

    def __post_init__(self) -> None:
        provider = str(self.provider_kind or "").strip().lower()
        selected_values = [_normalize_selection_value(item) for item in (self.selected_items or ())]
        selected_values.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))
        selected = tuple(selected_values)
        ignored = tuple(sorted({
            text for text in (_normalize_selection_string(item) for item in (self.ignore_files or ()))
            if text
        }))
        provider_selection = _normalize_selection_value(self.provider_selection or {})
        if not isinstance(provider_selection, dict):
            provider_selection = {}
        calculated = _selection_snapshot_hash(provider, selected, ignored, provider_selection)
        supplied = str(self.selection_hash or "").strip()
        object.__setattr__(self, "provider_kind", provider)
        object.__setattr__(self, "selected_items", selected)
        object.__setattr__(self, "ignore_files", ignored)
        object.__setattr__(self, "provider_selection", provider_selection)
        # Preserve a persisted/declared hash so validators can detect a forged
        # or corrupted snapshot instead of silently repairing it.
        object.__setattr__(self, "selection_hash", supplied or calculated)
        object.__setattr__(self, "_computed_hash", calculated)

    @property
    def computed_hash(self) -> str:
        return str(getattr(self, "_computed_hash", ""))

    @property
    def is_valid(self) -> bool:
        return not self.selection_hash or self.selection_hash == self.computed_hash

    @property
    def hash(self) -> str:
        """Short alias used by plan JSON and external integrations."""

        return self.selection_hash

    @classmethod
    def from_payload(cls, provider_kind: str, payload: Mapping[str, Any] | None) -> "SelectionSnapshot":
        """Extract and normalize all supported selection forms from a request.

        The function accepts a whole source record, its ``raw_data`` object,
        or an already serialized snapshot.  It intentionally ignores URL,
        password and other access fields so they can never enter the
        idempotency hash.
        """

        value = dict(payload or {})
        if isinstance(value.get("selection"), SelectionSnapshot):
            snapshot = value["selection"]
            if snapshot.provider_kind == str(provider_kind or "").strip().lower():
                return snapshot
        # A serialized snapshot is itself a valid payload.
        nested_snapshot = value.get("selection_snapshot")
        if isinstance(nested_snapshot, Mapping):
            value = {**value, **dict(nested_snapshot)}

        raw = value.get("raw_data")
        raw = dict(raw) if isinstance(raw, Mapping) else {}
        sources: list[Mapping[str, Any]] = [value, raw]
        for container in (value, raw):
            for key, candidate in container.items():
                if key == "selection" or key.endswith("_selection"):
                    if isinstance(candidate, Mapping):
                        sources.append(candidate)

        selected: Any = None
        ignored: list[str] = []
        selection_refs: list[dict[str, Any]] = []
        provider_payload: dict[str, Any] = {}
        selection_keys = {
            "selection",
            "selection_snapshot",
            "provider_selection",
            "native_selection",
            "quark_selection",
            "cloud139_selection",
            "tianyi_selection",
            "sixpan_selection",
        }
        for container in sources:
            # Keep the historical first-field behaviour for the adapter-facing
            # ``selected_items`` compatibility value, but never use it as the
            # complete canonical selection.  Every field is recorded below.
            for key in ("selected_items", "selected_files", "selected_folders", "selected_dirs", "item_refs"):
                if key in container and container.get(key) is not None:
                    if selected is None:
                        selected = container.get(key)
                    selection_refs.extend(_selection_refs_for_field(key, container.get(key)))
            candidate_ignored = container.get("ignore_files")
            if isinstance(candidate_ignored, (list, tuple, set)):
                ignored.extend(_normalize_selection_string(item) for item in candidate_ignored)
            elif candidate_ignored is not None:
                text = _normalize_selection_string(candidate_ignored)
                if text:
                    ignored.append(text)
            for key, candidate in container.items():
                if key in selection_keys or key.endswith("_selection"):
                    if isinstance(candidate, Mapping):
                        provider_payload[str(key)] = _normalize_selection_value(candidate)

        if selected is None:
            selected = []
        if isinstance(selected, Mapping):
            # Some adapters wrap entries under ``items``/``selected_items``.
            selected = selected.get("selected_items", selected.get("items", [selected]))
        if not isinstance(selected, (list, tuple)):
            selected = [selected]
        generic_selection = _selection_fields_from_refs(selection_refs)
        if generic_selection:
            # Preserve generic fields that appeared outside a named Provider
            # selection object.  This both freezes the complete selection for
            # execution and lets the hash be recomputed from persisted data.
            provider_payload["generic_selection"] = generic_selection
        return cls(
            provider_kind=str(provider_kind or "").strip().lower(),
            selected_items=tuple(selected),
            ignore_files=tuple(ignored),
            provider_selection=provider_payload,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None, *, provider_kind: str = "") -> "SelectionSnapshot":
        value = dict(payload or {})
        return cls(
            provider_kind=str(value.get("provider_kind") or provider_kind),
            selected_items=tuple(value.get("selected_items") or value.get("item_refs") or ()),
            ignore_files=tuple(value.get("ignore_files") or ()),
            provider_selection=dict(value.get("provider_selection") or {}),
            selection_hash=str(value.get("selection_hash") or value.get("hash") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_kind": self.provider_kind,
            "selected_items": [_copy_selection_value(item) for item in self.selected_items],
            "ignore_files": list(self.ignore_files),
            "provider_selection": _copy_selection_value(self.provider_selection),
            "selection_hash": self.selection_hash,
            "hash": self.selection_hash,
        }


def _normalize_selection_string(value: Any) -> str:
    return str(value or "").strip()


def _normalize_selection_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result = {str(key): _normalize_selection_value(value[key]) for key in sorted(value, key=lambda item: str(item))}
        for key in ("ignore_files",):
            if key in result:
                raw = result[key] if isinstance(result[key], list) else [result[key]]
                result[key] = sorted({text for text in (_normalize_selection_string(item) for item in raw) if text})
        for key in ("selected_items", "selected_files", "selected_folders", "selected_dirs", "item_refs"):
            if key in result and isinstance(result[key], list):
                result[key] = sorted(result[key], key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_normalize_selection_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _copy_selection_value(value: Any) -> Any:
    # The normalized value is JSON-compatible; round-tripping gives callers a
    # detached copy and avoids exposing internal mutable dictionaries.
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return str(value)


def _selection_snapshot_hash(
    provider_kind: str,
    selected_items: tuple[Any, ...],
    ignore_files: tuple[str, ...],
    provider_selection: dict[str, Any],
) -> str:
    # All generic fields are represented as typed refs before their nested
    # copies are stripped.  This distinguishes files/folders/dirs while still
    # keeping Provider-specific mode/base-directory data in the hash.
    selection_refs = _dedupe_selection_refs([
        *_selection_refs_for_field("selected_items", selected_items),
        *_selection_refs_from_value(provider_selection),
    ])
    canonical = {
        "provider_kind": provider_kind,
        "selection_refs": selection_refs,
        "ignore_files": list(ignore_files),
        "provider_selection": _strip_generic_selection_fields(provider_selection),
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_GENERIC_SELECTION_KEYS = {
    "selected_items",
    "selected_files",
    "selected_folders",
    "selected_dirs",
    "item_refs",
}


def _selection_field_values(value: Any) -> list[Any]:
    """Normalize one generic selection field to a list of item values."""

    if isinstance(value, Mapping):
        # Some clients/adapters wrap a field in ``items`` or the same field
        # name.  Unwrap only those known containers; arbitrary item mappings
        # remain one value.
        if "selected_items" in value:
            return _selection_field_values(value["selected_items"])
        if "items" in value:
            return _selection_field_values(value["items"])
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _selection_refs_for_field(key: str, value: Any) -> list[dict[str, Any]]:
    kind = str(key or "").strip().lower()
    if kind not in _GENERIC_SELECTION_KEYS:
        return []
    return [
        {"kind": kind, "value": _normalize_selection_value(item)}
        for item in _selection_field_values(value)
    ]


def _selection_refs_from_value(value: Any) -> list[dict[str, Any]]:
    """Recursively collect typed generic refs from provider selection data."""

    refs: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        for key, candidate in value.items():
            key_text = str(key).strip().lower()
            if key_text in _GENERIC_SELECTION_KEYS:
                refs.extend(_selection_refs_for_field(key_text, candidate))
            else:
                refs.extend(_selection_refs_from_value(candidate))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            refs.extend(_selection_refs_from_value(item))
    return refs


def _dedupe_selection_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for ref in refs:
        encoded = json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        unique[encoded] = ref
    return [unique[key] for key in sorted(unique)]


def _selection_fields_from_refs(refs: list[dict[str, Any]]) -> dict[str, list[Any]]:
    fields: dict[str, list[Any]] = {}
    for ref in _dedupe_selection_refs(refs):
        kind = str(ref.get("kind") or "")
        if kind in _GENERIC_SELECTION_KEYS:
            fields.setdefault(kind, []).append(_copy_selection_value(ref.get("value")))
    return fields


def _strip_generic_selection_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_generic_selection_fields(candidate)
            for key, candidate in value.items()
            if str(key) not in {"selected_items", "selected_files", "selected_folders", "selected_dirs", "item_refs", "ignore_files"}
        }
    if isinstance(value, list):
        return [_strip_generic_selection_fields(item) for item in value]
    return value


@dataclass(frozen=True)
class DestinationDescriptor:
    destination_id: str
    provider_kind: str
    target_revision: int = 1
    binding_id: str | None = None
    binding_revision: int | None = None
    account_id: str | None = None
    account_revision: int | None = None


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
    storage_identity_id: str = ""
    identity_root: str = "/"


@dataclass(frozen=True)
class NativeLocator:
    """同源原生保存或移动云/天翼原生 API 上传时由 Provider Adapter 解释的定位器。"""

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
class StorageTargetBinding:
    binding_id: str
    destination_id: str
    target_revision: int
    account_id: str | None
    account_revision: int | None
    provider_kind: str
    locator: dict[str, Any]
    enabled: bool
    current_revision: int


@dataclass(frozen=True)
class SourceDestinationAssignment:
    search_source_id: str
    category_key: str
    destination_id: str
    enabled: bool
    revision: int
    updated_at: str


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
    search_source_id: str = ""


@dataclass(frozen=True)
class ProviderManifest:
    provider_kind: str
    display_name: str
    contract_version: int
    account_schema: dict[str, Any]
    native_locator_schema: dict[str, Any] | None
    secret_fields: frozenset[str]
    components: frozenset[str]
    capabilities: "ProviderCapabilities"
    auth_methods: tuple[dict[str, Any], ...] = ()
    search_sources: tuple[dict[str, Any], ...] = ()
    icon_text: str = ""
    runtime_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProviderCapabilities:
    """Capabilities advertised by a Provider Adapter.

    These flags describe the adapter contract only.  OpenList read/write
    capability remains a property of the frozen mount/path and is validated
    separately by the planner.  Keep fields keyword-only at call sites: the
    registry serializes this object into API manifests and positional
    construction silently breaks when a new capability is added.
    """

    supports_search: bool = False
    supports_share_inspection: bool = False
    supports_native_save: bool = False
    supports_native_offline: bool = False
    supports_operation_polling: bool = False
    supports_native_verify: bool = False
    supports_source_materialization: bool = False
    supports_relay_upload: bool = False
    supports_rclone_dest: bool = False
    supports_direct_final: bool = False
    supports_quota: bool = False


@dataclass(frozen=True)
class AccountTestResult:
    ok: bool
    identity_hint: str = ""
    message: str = ""
    errors: list[str] = field(default_factory=list)
    # A trusted Provider subject, never a nickname or an editable display hint.
    identity_id: str = ""
