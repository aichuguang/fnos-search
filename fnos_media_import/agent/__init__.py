"""Agent-facing capability surface (MCP server) for fnos-search.

The package keeps the protocol and the business wiring apart:

- ``services`` describes the callables the Flask application injects.
- ``registry`` describes the tools, their scopes and their risk level.
- ``mcp`` adapts those tools to JSON-RPC 2.0 over HTTP.

``create_agent_blueprint`` is the only entry point the application needs.
"""

from __future__ import annotations

from .mcp.blueprint import create_agent_blueprint
from .services import AgentServices

__all__ = ["AgentServices", "create_agent_blueprint"]
