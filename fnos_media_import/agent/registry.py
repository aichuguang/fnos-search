"""Tool registry: the single description of what the agent may do."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .context import AgentCallContext

RISK_READ = "read"
RISK_WRITE = "write"
RISK_DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    scope: str
    handler: Callable[[AgentCallContext, dict[str, Any]], dict[str, Any]]
    risk: str = RISK_READ
    idempotent: bool = True

    def annotations(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "readOnlyHint": self.risk == RISK_READ,
            "destructiveHint": self.risk == RISK_DESTRUCTIVE,
            "idempotentHint": self.idempotent,
        }

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations(),
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"duplicate agent tool: {spec.name}")
        self._tools[spec.name] = spec
        return spec

    def extend(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(str(name or ""))

    def all(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda item: item.name)

    def describe(self, scopes: frozenset[str] | set[str]) -> list[dict[str, Any]]:
        return [spec.describe() for spec in self.all() if spec.scope in scopes]

    def __len__(self) -> int:
        return len(self._tools)


@dataclass
class ToolCallResult:
    payload: dict[str, Any]
    is_error: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
