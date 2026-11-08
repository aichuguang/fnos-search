"""JSON-RPC 2.0 framing for the MCP endpoint.

Only the transport-neutral envelope lives here; method dispatch is in
``handlers``. No third-party dependency is required.
"""

from __future__ import annotations

from typing import Any

JSONRPC_VERSION = "2.0"

# Newest first. ``initialize`` echoes the client version when it is one we
# understand so older clients keep working unchanged.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Application level codes kept outside the JSON-RPC reserved range.
UNAUTHORIZED = -32001
FORBIDDEN = -32003
RATE_LIMITED = -32029
SERVER_BUSY = -32030


def success(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def failure(request_id: Any, code: int, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": int(code), "message": str(message)}
    if data:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def is_notification(payload: dict[str, Any]) -> bool:
    return "id" not in payload


def validate_request(payload: Any) -> str:
    """Return an error message when the envelope is malformed, else ``""``."""

    if not isinstance(payload, dict):
        return "请求必须是 JSON 对象"
    if payload.get("jsonrpc") != JSONRPC_VERSION:
        return "jsonrpc 必须为 2.0"
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        return "缺少 method"
    params = payload.get("params")
    if params is not None and not isinstance(params, (dict, list)):
        return "params 必须是对象或数组"
    return ""
