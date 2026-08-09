from __future__ import annotations

from importlib import metadata
import re
from collections.abc import Iterable as IterableABC
from typing import Any, Iterable

from ..domain.models import AccountTestResult, ProviderCapabilities, ProviderManifest
from .base import ProviderAdapter, manifest_dict, validate_adapter_contract


class ProviderRegistryError(RuntimeError):
    pass


class ProviderRegistry:
    """Runtime registry for built-in and externally installed Provider plugins."""

    def __init__(self, adapters: Iterable[ProviderAdapter] | None = None) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}
        self._loaded_entry_points: set[str] = set()
        for adapter in adapters or ():
            self.register(adapter)

    def register(self, adapter: ProviderAdapter) -> None:
        provider_manifest = self._validate(adapter)
        if provider_manifest.provider_kind in self._adapters:
            raise ProviderRegistryError(
                f"Provider 重复注册：{provider_manifest.provider_kind}"
            )
        self._adapters[provider_manifest.provider_kind] = adapter

    def get(self, provider_kind: str) -> ProviderAdapter:
        try:
            return self._adapters[str(provider_kind)]
        except KeyError as exc:
            raise ProviderRegistryError(f"Provider 未注册：{provider_kind}") from exc

    def has(self, provider_kind: str) -> bool:
        return str(provider_kind or "").strip() in self._adapters

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def adapter_map(self) -> dict[str, ProviderAdapter]:
        """Return a shallow copy for executor construction without exposing internals."""

        return dict(self._adapters)

    def manifests(self) -> list[dict[str, Any]]:
        return [manifest_dict(self._adapters[key].manifest) for key in sorted(self._adapters)]

    def search_sources(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        seen_types: dict[str, str] = {}
        for provider_kind in sorted(self._adapters):
            provider_manifest = self._adapters[provider_kind].manifest
            for raw in provider_manifest.search_sources:
                item = dict(raw)
                source_id = str(item.get("search_source_id") or "").strip()
                if not source_id:
                    raise ProviderRegistryError(
                        f"Provider {provider_kind} 声明了空的 search_source_id"
                    )
                if source_id in seen:
                    raise ProviderRegistryError(f"搜索来源重复注册：{source_id}")
                seen.add(source_id)
                item["search_source_id"] = source_id
                item["provider_kind"] = provider_kind
                item.setdefault("label", provider_manifest.display_name)
                item.setdefault("source_form", "share")
                item.setdefault("icon_text", provider_manifest.icon_text)
                source_form = str(item.get("source_form") or "").strip()
                if source_form not in {"share", "magnet", "local", "http"}:
                    raise ProviderRegistryError(
                        f"Provider {provider_kind} 搜索来源 {source_id} source_form 非法：{source_form}"
                    )
                source_types = item.get("source_types") or []
                if not isinstance(source_types, (list, tuple, set, frozenset)):
                    raise ProviderRegistryError(
                        f"Provider {provider_kind} 搜索来源 {source_id} source_types 必须是数组"
                    )
                normalized_types = [str(value).strip() for value in source_types if str(value).strip()]
                item["source_types"] = list(dict.fromkeys(normalized_types))
                for source_type in item["source_types"]:
                    owner = seen_types.get(source_type)
                    if owner is not None and owner != source_id:
                        raise ProviderRegistryError(
                            f"来源类型重复注册：{source_type} ({owner}/{source_id})"
                        )
                    seen_types[source_type] = source_id
                result.append(item)
        return result

    def account_tester(
        self,
        provider_kind: str,
        credentials: dict[str, Any],
        config: dict[str, Any],
    ) -> AccountTestResult:
        """Run the registered Adapter's validation and real account probe.

        This callable matches ``ProviderAccountService``'s tester contract and
        removes provider-specific ``if/elif`` branches from the composition root.
        """

        adapter = self.get(provider_kind)
        errors = list(adapter.validate_account_config(dict(config or {}), dict(credentials or {})) or [])
        if errors:
            return AccountTestResult(False, message="配置校验失败", errors=errors)
        value = adapter.test_account(
            {
                "provider_kind": str(provider_kind),
                "config": dict(config or {}),
                "credentials": dict(credentials or {}),
            }
        )
        if isinstance(value, AccountTestResult):
            return value
        if isinstance(value, dict):
            return AccountTestResult(
                bool(value.get("ok") or value.get("success")),
                identity_hint=str(value.get("identity_hint") or value.get("user") or ""),
                message=str(value.get("message") or ""),
                errors=list(value.get("errors") or []),
            )
        return AccountTestResult(
            bool(getattr(value, "ok", False)),
            identity_hint=str(getattr(value, "identity_hint", "") or ""),
            message=str(getattr(value, "message", "") or ""),
            errors=list(getattr(value, "errors", []) or []),
        )

    def load_entry_points(self, context: dict[str, Any] | None = None) -> list[str]:
        """Load plugins from the ``fnos_media_import.providers`` entry-point group.

        Plugins expose ``register(registry, context)`` or are themselves callable.
        This is the stable extension point for adding providers such as 115.
        """
        loaded: list[str] = []
        entries = metadata.entry_points()
        selected = (
            entries.select(group="fnos_media_import.providers")
            if hasattr(entries, "select")
            else entries.get("fnos_media_import.providers", [])
        )
        for entry in sorted(selected, key=lambda item: str(item.name)):
            entry_name = str(entry.name)
            if entry_name in self._loaded_entry_points:
                continue
            snapshot = dict(self._adapters)
            try:
                plugin = entry.load()
                register = getattr(plugin, "register", plugin)
                if not callable(register):
                    raise ProviderRegistryError(
                        f"Provider 插件没有 register 入口：{entry.name}"
                    )
                result = register(self, dict(context or {}))
                if result is not None:
                    returned = (
                        list(result)
                        if isinstance(result, IterableABC) and not isinstance(result, (str, bytes, dict))
                        else [result]
                    )
                    for adapter in returned:
                        self.register(adapter)
            except Exception as exc:
                # One broken plugin must never leave a partially registered
                # Provider graph behind.
                self._adapters = snapshot
                if isinstance(exc, ProviderRegistryError):
                    raise
                raise ProviderRegistryError(
                    f"Provider 插件加载失败：{entry_name}: {exc}"
                ) from exc
            self._loaded_entry_points.add(entry_name)
            loaded.append(entry_name)
        return loaded

    @staticmethod
    def _validate(adapter: ProviderAdapter) -> ProviderManifest:
        value = getattr(adapter, "manifest", None)
        if not isinstance(value, ProviderManifest):
            raise ProviderRegistryError("Adapter 缺少合法 ProviderManifest")
        if value.contract_version != 1:
            raise ProviderRegistryError(
                f"不支持的 Adapter contract_version：{value.contract_version}"
            )
        if not value.provider_kind:
            raise ProviderRegistryError("Provider manifest 缺少 provider_kind")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", str(value.provider_kind)):
            raise ProviderRegistryError(
                f"Provider manifest provider_kind 非法：{value.provider_kind}"
            )
        adapter_kind = str(getattr(adapter, "kind", value.provider_kind) or "").strip()
        if adapter_kind != value.provider_kind:
            raise ProviderRegistryError(
                f"Adapter kind 与 manifest.provider_kind 不一致：{adapter_kind}/{value.provider_kind}"
            )
        missing = validate_adapter_contract(adapter)
        if missing:
            raise ProviderRegistryError(
                f"Adapter {value.provider_kind} 缺少方法：{', '.join(missing)}"
            )
        runtime_capabilities = adapter.capabilities(account=None)
        if not isinstance(runtime_capabilities, (ProviderCapabilities, dict)):
            raise ProviderRegistryError(
                f"Adapter {value.provider_kind} capabilities() 返回格式无效"
            )
        runtime_capability_values = (
            runtime_capabilities
            if isinstance(runtime_capabilities, dict)
            else {
                name: getattr(runtime_capabilities, name)
                for name in ProviderCapabilities.__dataclass_fields__
            }
        )
        for name, enabled in runtime_capability_values.items():
            if bool(enabled) and not bool(getattr(value.capabilities, name, False)):
                raise ProviderRegistryError(
                    f"Adapter {value.provider_kind} 运行时能力 {name}=true 超出 manifest 声明"
                )
        account_schema = value.account_schema
        if not isinstance(account_schema, dict):
            raise ProviderRegistryError(
                f"Provider {value.provider_kind} account_schema 必须是对象"
            )
        if not value.auth_methods and not account_schema.get("fields"):
            raise ProviderRegistryError("Provider manifest 必须声明登录方式或账号字段")
        ProviderRegistry._validate_fields(
            value.provider_kind,
            account_schema,
            schema_name="account_schema",
        )
        if value.native_locator_schema is not None:
            ProviderRegistry._validate_fields(
                value.provider_kind,
                value.native_locator_schema,
                schema_name="native_locator_schema",
            )
        if value.runtime_schema is not None:
            ProviderRegistry._validate_fields(
                value.provider_kind,
                value.runtime_schema,
                schema_name="runtime_schema",
            )
        auth_kinds: set[str] = set()
        for auth in value.auth_methods:
            if not isinstance(auth, dict):
                raise ProviderRegistryError(
                    f"Provider {value.provider_kind} auth_method 必须是对象"
                )
            auth_kind = str(auth.get("kind") or "").strip()
            if not auth_kind or auth_kind in auth_kinds:
                raise ProviderRegistryError(
                    f"Provider {value.provider_kind} auth_method kind 重复或为空"
                )
            auth_kinds.add(auth_kind)
            input_schema = auth.get("input_schema")
            if input_schema is not None:
                ProviderRegistry._validate_fields(
                    value.provider_kind,
                    input_schema,
                    schema_name=f"auth_methods[{auth_kind}].input_schema",
                )
        schema_secret_fields = {
            str(field.get("name") or "")
            for field in account_schema.get("fields") or []
            if isinstance(field, dict) and field.get("secret")
        }
        if not schema_secret_fields.issubset(value.secret_fields):
            missing_secret_names = sorted(schema_secret_fields - set(value.secret_fields))
            raise ProviderRegistryError(
                f"Provider {value.provider_kind} secret_fields 缺少：{', '.join(missing_secret_names)}"
            )
        # Keep the manifest and capability surface internally coherent.  A
        # plugin may expose a method proactively, but it must not advertise a
        # capability that its manifest/components explicitly omit (or vice
        # versa for operation capabilities).
        capability_component_pairs = {
            "supports_native_save": "native_save",
            "supports_native_offline": "native_offline",
            "supports_source_materialization": "materialize",
            "supports_relay_upload": "relay",
        }
        for capability, component in capability_component_pairs.items():
            advertised = bool(getattr(value.capabilities, capability, False))
            if advertised and component not in value.components:
                raise ProviderRegistryError(
                    f"Provider {value.provider_kind} capabilities.{capability}=true "
                    f"但 manifest.components 缺少 {component}"
                )
        return value

    @staticmethod
    def _validate_fields(
        provider_kind: str,
        schema: Any,
        *,
        schema_name: str,
    ) -> None:
        if not isinstance(schema, dict):
            raise ProviderRegistryError(
                f"Provider {provider_kind} {schema_name} 必须是对象"
            )
        fields = schema.get("fields")
        if not isinstance(fields, (list, tuple)):
            raise ProviderRegistryError(
                f"Provider {provider_kind} {schema_name}.fields 必须是数组"
            )
        names: set[str] = set()
        for field in fields:
            if not isinstance(field, dict):
                raise ProviderRegistryError(
                    f"Provider {provider_kind} {schema_name} 字段必须是对象"
                )
            name = str(field.get("name") or "").strip()
            if not name or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", name):
                raise ProviderRegistryError(
                    f"Provider {provider_kind} {schema_name} 字段名非法：{name}"
                )
            if name in names:
                raise ProviderRegistryError(
                    f"Provider {provider_kind} {schema_name} 字段重复：{name}"
                )
            names.add(name)
            if field.get("secret") and field.get("configured_flag"):
                flag = str(field["configured_flag"]).strip()
                if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", flag):
                    raise ProviderRegistryError(
                        f"Provider {provider_kind} {schema_name} configured_flag 非法：{flag}"
                    )


__all__ = ["ProviderRegistry", "ProviderRegistryError"]
