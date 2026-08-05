from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class SearchProviderConfig:
    key: str
    name: str
    enabled: bool = True
    priority: int = 100
    capabilities: dict[str, Any] = field(default_factory=dict)


class SearchProvider(Protocol):
    key: str
    name: str
    priority: int

    def is_enabled(self) -> bool:
        ...

    def describe(self) -> dict[str, Any]:
        ...

    def search(self, keyword: str, sources: list[str] | None = None, token: str = "", options: dict[str, Any] | None = None) -> dict[str, Any]:
        ...
