"""Flask blueprint exposing the MCP endpoint at ``/mcp``.

Transport notes:

* POST only, stateless, JSON responses (no SSE) so a tool call never pins a
  gunicorn thread between requests.
* A bounded semaphore caps concurrent tool execution so a slow search cannot
  starve the admin UI.
* Per-token rate limiting is in-process; the web container runs a single
  gunicorn worker, which makes that exact rather than approximate.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any

from flask import Blueprint, jsonify, request

from ..audit import AgentAuditLog
from ..auth import AgentTokenRecord, AgentTokenStore
from ..errors import rate_limited, server_busy
from ..registry import ToolRegistry
from ..services import AgentServices
from . import protocol
from .handlers import SERVER_NAME, SERVER_VERSION, McpHandler

logger = logging.getLogger(__name__)

DEFAULT_RATE_LIMIT = 60
RATE_WINDOW_SECONDS = 60


class _TokenRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[int, deque[float]] = defaultdict(deque)

    def retry_after(self, token_id: int, limit: int) -> int:
        """Return 0 when the call is allowed, else seconds until a slot frees."""

        if limit <= 0:
            return 0
        now = time.monotonic()
        cutoff = now - RATE_WINDOW_SECONDS
        with self._lock:
            bucket = self._buckets[token_id]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(1, int(RATE_WINDOW_SECONDS - (now - bucket[0])) + 1)
            bucket.append(now)
            return 0


def create_agent_blueprint(
    *,
    registry: ToolRegistry,
    services: AgentServices,
    token_store: AgentTokenStore,
    audit: AgentAuditLog | None = None,
    enabled: bool = True,
    max_concurrency: int = 2,
    default_rate_limit: int = DEFAULT_RATE_LIMIT,
) -> Blueprint:
    blueprint = Blueprint("agent_mcp", __name__)
    handler = McpHandler(
        registry=registry,
        services=services,
        token_store=token_store,
        audit=audit,
        enabled=enabled,
    )
    limiter = _TokenRateLimiter()
    gate = threading.BoundedSemaphore(max(1, int(max_concurrency or 1)))

    def _authenticate() -> tuple[AgentTokenRecord | None, dict[str, Any] | None]:
        header = str(request.headers.get("Authorization") or "")
        if not header.lower().startswith("bearer "):
            return None, protocol.failure(
                None,
                protocol.UNAUTHORIZED,
                "缺少 Bearer 令牌",
                {"code": "UNAUTHORIZED", "hint": "使用 Authorization: Bearer fnos_agent_..."},
            )
        plaintext = header.split(" ", 1)[1].strip()
        record = token_store.verify(plaintext)
        if record is None:
            return None, protocol.failure(
                None,
                protocol.UNAUTHORIZED,
                "Agent 令牌无效、过期或已撤销",
                {"code": "UNAUTHORIZED", "hint": "在管理后台重新生成令牌"},
            )
        return record, None

    def _dispatch_one(payload: Any, token: AgentTokenRecord) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return protocol.failure(None, protocol.INVALID_REQUEST, "请求必须是 JSON 对象")
        return handler.handle(payload, token)

    @blueprint.post("/mcp")
    def mcp_endpoint():
        token, auth_error = _authenticate()
        if token is None:
            return jsonify(auth_error), 401

        retry_after = limiter.retry_after(token.id, int(token.rate_limit or default_rate_limit))
        if retry_after:
            error = rate_limited(retry_after)
            response = protocol.failure(None, protocol.RATE_LIMITED, error.message, error.to_payload())
            return jsonify(response), 429, {"Retry-After": str(retry_after)}

        acquired = gate.acquire(blocking=False)
        if not acquired:
            error = server_busy(int(max_concurrency or 1))
            response = protocol.failure(None, protocol.SERVER_BUSY, error.message, error.to_payload())
            return jsonify(response), 503, {"Retry-After": "3"}

        try:
            payload = request.get_json(silent=True)
            if payload is None:
                return jsonify(protocol.failure(None, protocol.PARSE_ERROR, "请求体不是合法 JSON")), 400

            if isinstance(payload, list):
                responses = [item for item in (_dispatch_one(entry, token) for entry in payload) if item is not None]
                if not responses:
                    return ("", 202)
                return jsonify(responses), 200

            response = _dispatch_one(payload, token)
            if response is None:
                return ("", 202)
            return jsonify(response), 200
        finally:
            gate.release()

    @blueprint.get("/mcp")
    def mcp_info():
        return jsonify(
            {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
                "transport": "http-post",
                "enabled": bool(enabled),
                "toolCount": len(registry),
                "note": "只接受 POST JSON-RPC 2.0；无需会话 ID。",
            }
        )

    @blueprint.delete("/mcp")
    def mcp_close():
        # Stateless transport: nothing to tear down, but clients may send it.
        return ("", 204)

    @blueprint.route("/mcp", methods=["OPTIONS"])
    def mcp_options():
        response = jsonify({"allow": ["POST", "GET", "DELETE", "OPTIONS"]})
        response.headers["Allow"] = "POST, GET, DELETE, OPTIONS"
        return response

    return blueprint
