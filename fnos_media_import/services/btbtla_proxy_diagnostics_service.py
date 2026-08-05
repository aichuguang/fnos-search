from __future__ import annotations

from typing import Any, Callable

from ..providers.btbtla import BtbtlaClient, _redact_proxy_credentials


SECRET_PLACEHOLDERS = {"", "***", "******", "已配置，留空不修改"}
BTBTLA_TEST_FIELDS = {
    "base_url",
    "timeout",
    "request_retries",
    "retry_delay_seconds",
    "verify_tls",
    "use_env_proxy",
    "proxy_enabled",
    "proxy_url",
    "user_agent",
}


class BtbtlaProxyDiagnosticsService:
    """Builds a temporary BTBTLA client so unsaved proxy form values can be tested safely."""

    def __init__(
        self,
        *,
        current_config: Callable[[], dict[str, Any]],
        current_routes: Callable[[], dict[str, Any]],
    ) -> None:
        self.current_config = current_config
        self.current_routes = current_routes

    def test(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self.current_config()
        current = dict(current) if isinstance(current, dict) else {}
        override = payload.get("btbtla") if isinstance(payload, dict) and isinstance(payload.get("btbtla"), dict) else payload
        override = override if isinstance(override, dict) else {}
        candidate = dict(current)
        for field in BTBTLA_TEST_FIELDS:
            if field not in override:
                continue
            value = override.get(field)
            if field == "proxy_url" and str(value or "").strip() in SECRET_PLACEHOLDERS:
                continue
            candidate[field] = value
        routes = self.current_routes()
        routes = routes if isinstance(routes, dict) else {}
        client: BtbtlaClient | None = None
        try:
            client = BtbtlaClient(candidate, routes)
            result = client.probe_connection()
            result["tested_unsaved_values"] = bool(override)
            return result
        except Exception as exc:  # noqa: BLE001
            message = _redact_proxy_credentials(str(exc) or exc.__class__.__name__)
            return {
                "success": False,
                "message": message,
                "mode": "explicit" if candidate.get("proxy_enabled") else "environment" if candidate.get("use_env_proxy") else "direct",
                "proxy_applied": False,
                "proxy": {},
                "target": {},
                "ip": {},
                "warnings": [],
                "tested_unsaved_values": bool(override),
            }
        finally:
            if client is not None:
                client.close()
