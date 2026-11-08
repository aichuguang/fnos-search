"""Per-call context handed to every tool handler."""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import scope_denied
from .services import AgentServices


@dataclass(frozen=True)
class AgentCallContext:
    token_id: int
    token_name: str
    scopes: frozenset[str]
    services: AgentServices
    request_id: str = ""
    dry_run: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

    def has(self, scope: str) -> bool:
        return scope in self.scopes or "*" in self.scopes

    def require(self, scope: str) -> None:
        if not self.has(scope):
            raise scope_denied(scope)
