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


__all__ = ["ActiveProviderImporter", "ActiveProviderUnavailable"]
