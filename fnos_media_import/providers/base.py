from __future__ import annotations

from dataclasses import asdict
from typing import Any, Protocol

from ..domain.models import AccountTestResult, ProviderCapabilities, ProviderManifest


class ProviderAdapter(Protocol):
    manifest: ProviderManifest

    def capabilities(self, account: dict[str, Any] | None = None) -> ProviderCapabilities: ...
    def validate_account_config(self, config: dict[str, Any], credentials: dict[str, Any]) -> list[str]: ...
    def test_account(self, account: dict[str, Any]) -> AccountTestResult: ...
    def inspect_source(self, account: dict[str, Any], locator: dict[str, Any]) -> dict[str, Any]: ...
    def can_native_transfer(self, source: Any, destination: Any, account: Any) -> Any: ...
    def native_save(self, request: Any) -> Any: ...
    def native_offline(self, request: Any) -> Any: ...
    def materialize_source(self, request: Any) -> Any: ...
    def poll_operation(self, receipt: Any) -> Any: ...
    def verify_native_result(self, request: Any) -> Any: ...
    def cancel_operation(self, receipt: Any) -> Any: ...

    def relay_uploader(self) -> Any: ...


# Methods which are part of the stable adapter surface regardless of the
# selected transfer strategy. ``search`` is intentionally not in this list:
# the import/runtime adapter may be backed by a separate search service while
# still being the canonical account/transfer adapter.
BASE_ADAPTER_METHODS = frozenset(
    {
        "capabilities",
        "validate_account_config",
        "test_account",
        "inspect_source",
        "can_native_transfer",
    }
)


def required_adapter_methods(manifest: ProviderManifest) -> frozenset[str]:
    """Return methods required by a manifest's advertised capabilities."""

    required = set(BASE_ADAPTER_METHODS)
    caps = manifest.capabilities
    if caps.supports_native_save:
        required.add("native_save")
    if caps.supports_native_offline:
        required.add("native_offline")
    if caps.supports_source_materialization:
        required.add("materialize_source")
    if caps.supports_native_verify:
        required.add("verify_native_result")
    if caps.supports_relay_upload:
        required.add("relay_uploader")
    if caps.supports_operation_polling or any(
        (
            caps.supports_native_save,
            caps.supports_native_offline,
            caps.supports_source_materialization,
            caps.supports_relay_upload,
        )
    ):
        required.update({"poll_operation", "cancel_operation"})
    return frozenset(required)


def validate_adapter_contract(adapter: ProviderAdapter) -> list[str]:
    """Return missing adapter methods for its manifest, without side effects."""

    value = getattr(adapter, "manifest", None)
    if not isinstance(value, ProviderManifest):
        return ["manifest"]
    return sorted(
        name
        for name in required_adapter_methods(value)
        if not callable(getattr(adapter, name, None))
    )


def manifest_dict(manifest: ProviderManifest) -> dict[str, Any]:
    data = asdict(manifest)
    data["secret_fields"] = sorted(manifest.secret_fields)
    data["components"] = sorted(manifest.components)
    data["capabilities"] = asdict(manifest.capabilities)
    return data


def manifest(
    kind: str,
    display_name: str,
    fields: list[dict[str, Any]],
    *,
    components: set[str],
    capabilities: ProviderCapabilities,
    locator_fields: list[dict[str, Any]] | None = None,
    auth_methods: list[dict[str, Any]] | None = None,
    search_sources: list[dict[str, Any]] | None = None,
    runtime_fields: list[dict[str, Any]] | None = None,
    managed_secret_fields: set[str] | frozenset[str] | None = None,
    icon_text: str = "",
) -> ProviderManifest:
    secrets = frozenset(
        {
            *(str(field["name"]) for field in fields if field.get("secret")),
            *(str(name) for name in (managed_secret_fields or ()) if str(name).strip()),
        }
    )
    return ProviderManifest(
        provider_kind=kind,
        display_name=display_name,
        contract_version=1,
        account_schema={"fields": fields},
        native_locator_schema={"fields": locator_fields or []} if locator_fields is not None else None,
        secret_fields=secrets,
        components=frozenset(components),
        capabilities=capabilities,
        auth_methods=tuple(auth_methods or ()),
        search_sources=tuple(search_sources or ()),
        icon_text=str(icon_text or ""),
        runtime_schema={"fields": runtime_fields or []} if runtime_fields is not None else None,
    )


__all__ = [
    "ProviderAdapter",
    "BASE_ADAPTER_METHODS",
    "required_adapter_methods",
    "validate_adapter_contract",
    "manifest",
    "manifest_dict",
]
