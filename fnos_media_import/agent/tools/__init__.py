"""Builds the tool registry from the per-domain modules."""

from __future__ import annotations

from ..registry import ToolRegistry
from ..services import AgentServices
from . import (
    discovery,
    imports_write,
    jobs,
    media,
    organizer,
    organizer_write,
    ops,
    scheduling,
    scheduling_write,
    trending,
)

_MODULES = (
    discovery,
    jobs,
    organizer,
    scheduling,
    ops,
    imports_write,
    organizer_write,
    scheduling_write,
    media,
    trending,
)


def build_registry(services: AgentServices | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    for module in _MODULES:
        module.register(registry, services)
    return registry


__all__ = ["build_registry"]
