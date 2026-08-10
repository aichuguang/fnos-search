"""Account-selection aware Provider importer proxies.

Provider credentials live in encrypted account revisions.  Long-lived
services therefore hold a proxy rather than a credential-bearing importer;
each operation resolves the currently activated account revision.  A missing
selection is an explicit configuration error and never falls back to a global
configuration section.
"""

from __future__ import annotations

from typing import Any, Callable


class ActiveProviderUnavailable(RuntimeError):
    """Raised when a Provider has no usable activated account."""


class ActiveProviderImporter:
    """Lazily delegate importer methods to an activated Provider account."""

    def __init__(self, provider_kind: str, resolver: Callable[[], Any]) -> None:
        self.provider_kind = str(provider_kind or "provider").strip() or "provider"
        self._resolver = resolver

    def resolve(self) -> Any:
        try:
            importer = self._resolver()
        except ActiveProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ActiveProviderUnavailable(
                f"当前{self.provider_kind}账号不可用：{str(exc)[:300]}"
            ) from exc
        if importer is None:
            raise ActiveProviderUnavailable(
                f"当前{self.provider_kind}账号未配置或未激活"
            )
        return importer

    @property
    def configured(self) -> bool:
        try:
            importer = self.resolve()
        except ActiveProviderUnavailable:
            return False
        value = getattr(importer, "configured", None)
        if value is not None:
            return bool(value)
        checker = getattr(importer, "check_configuration", None)
        if callable(checker):
            try:
                result = checker()
                return bool(getattr(result, "ok", result.get("ok") if isinstance(result, dict) else result))
            except Exception:  # noqa: BLE001
                return False
        return True

    def check_configuration(self) -> Any:
        importer = self.resolve()
        checker = getattr(importer, "check_configuration", None)
        if callable(checker):
            return checker()
        return {"ok": True, "status": "ready"}

    def close(self) -> None:
        """Do not close the account importer owned by another request."""

    def __getattr__(self, name: str) -> Any:
        return getattr(self.resolve(), name)


def get_optional_provider_method(importer: Any, name: str) -> Callable[..., Any] | None:
    """Return an optional importer method without failing on an inactive proxy.

    ``hasattr`` is unsafe for :class:`ActiveProviderImporter` because Python
    implements it through attribute access, which invokes ``__getattr__`` and
    resolves the active account.  Read-only capability discovery must fail
    closed instead of turning an unconfigured Provider into an HTTP 500.
    """

    if importer is None:
        return None
    try:
        value = getattr(importer, str(name or ""), None)
    except ActiveProviderUnavailable:
        return None
    return value if callable(value) else None


__all__ = [
    "ActiveProviderImporter",
    "ActiveProviderUnavailable",
    "get_optional_provider_method",
]
