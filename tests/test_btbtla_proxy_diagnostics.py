from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from fnos_media_import.config_persistence import normalize_advanced_config_payload
from fnos_media_import.providers.btbtla import BtbtlaClient
from fnos_media_import.services.btbtla_proxy_diagnostics_service import BtbtlaProxyDiagnosticsService


class BtbtlaProxyProbeTests(unittest.TestCase):
    def test_transient_connection_reset_is_retried_before_search_fails(self) -> None:
        client = BtbtlaClient(
            {
                "base_url": "https://www.btbtla.com",
                "request_retries": 2,
                "retry_delay_seconds": 0,
            },
            {},
        )
        response = Mock(status_code=200, encoding="utf-8", apparent_encoding="utf-8", text="<html></html>")
        client.session.get = Mock(
            side_effect=[
                requests.exceptions.ConnectionError(
                    "Connection aborted: ConnectionResetError(10054)",
                ),
                response,
            ]
        )
        client.session.close = Mock()
        try:
            with patch("fnos_media_import.providers.btbtla.time.sleep") as sleep:
                result = client.search("仙逆")
        finally:
            client.close()

        self.assertEqual(result["items"], [])
        self.assertEqual(client.session.get.call_count, 2)
        self.assertGreaterEqual(client.session.close.call_count, 2)
        sleep.assert_not_called()
        response.close.assert_called_once()

    def test_non_retryable_socks_dependency_error_fails_immediately(self) -> None:
        client = BtbtlaClient(
            {
                "base_url": "https://www.btbtla.com",
                "request_retries": 2,
                "retry_delay_seconds": 0,
            },
            {},
        )
        client.session.get = Mock(
            side_effect=requests.exceptions.InvalidSchema("Missing dependencies for SOCKS support."),
        )
        try:
            with self.assertRaises(requests.exceptions.InvalidSchema):
                client.search("仙逆")
        finally:
            client.close()

        client.session.get.assert_called_once()

    def test_probe_reports_attempt_count_after_transient_retry(self) -> None:
        client = BtbtlaClient(
            {
                "base_url": "https://www.btbtla.com",
                "request_retries": 2,
                "retry_delay_seconds": 0,
            },
            {},
        )
        response = Mock(status_code=200, url="https://www.btbtla.com/")
        client.session.get = Mock(
            side_effect=[
                requests.exceptions.ConnectionError("Connection reset by peer"),
                response,
            ]
        )
        client.session.close = Mock()
        try:
            with patch("fnos_media_import.providers.btbtla._probe_public_ip", return_value=("", "not tested")):
                result = client.probe_connection()
        finally:
            client.close()

        self.assertTrue(result["success"])
        self.assertEqual(result["target"]["attempts"], 2)
        self.assertEqual(client.session.get.call_count, 2)

    def test_socks5h_probe_reports_remote_dns_and_changed_exit_ip_without_credentials(self) -> None:
        client = BtbtlaClient(
            {
                "base_url": "https://www.btbtla.com",
                "timeout": 15,
                "verify_tls": False,
                "proxy_enabled": True,
                "proxy_url": "socks5h://proxy-user:proxy-pass@proxy.local:1080",
            },
            {},
        )
        client.session.get = Mock(
            return_value=SimpleNamespace(
                status_code=200,
                url="https://www.btbtla.com/",
            )
        )
        connection = Mock()
        try:
            with (
                patch("fnos_media_import.providers.btbtla.socket.create_connection", return_value=connection) as create_connection,
                patch(
                    "fnos_media_import.providers.btbtla._probe_public_ip",
                    side_effect=[("198.51.100.10", ""), ("203.0.113.20", "")],
                ),
            ):
                result = client.probe_connection()
        finally:
            client.close()

        self.assertTrue(result["success"])
        self.assertTrue(result["proxy_applied"])
        self.assertEqual(result["proxy"]["display"], "socks5h://proxy.local:1080")
        self.assertEqual(result["proxy"]["dns_mode"], "remote")
        self.assertTrue(result["proxy"]["authentication"])
        self.assertTrue(result["proxy"]["tcp_reachable"])
        self.assertEqual(result["target"]["status_code"], 200)
        self.assertEqual(result["ip"]["tested_path"], "198.51.100.10")
        self.assertEqual(result["ip"]["direct"], "203.0.113.20")
        self.assertTrue(result["ip"]["changed"])
        create_connection.assert_called_once_with(("proxy.local", 1080), timeout=5.0)
        connection.close.assert_called_once()
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("proxy-user", serialized)
        self.assertNotIn("proxy-pass", serialized)

    def test_unreachable_proxy_stops_before_btbtla_request(self) -> None:
        client = BtbtlaClient(
            {
                "base_url": "https://www.btbtla.com",
                "proxy_enabled": True,
                "proxy_url": "socks5h://proxy.local:1080",
            },
            {},
        )
        client.session.get = Mock()
        try:
            with patch(
                "fnos_media_import.providers.btbtla.socket.create_connection",
                side_effect=OSError("connection refused"),
            ):
                result = client.probe_connection()
        finally:
            client.close()

        self.assertFalse(result["success"])
        self.assertFalse(result["proxy_applied"])
        self.assertFalse(result["proxy"]["tcp_reachable"])
        self.assertIn("无法连接代理服务", result["message"])
        client.session.get.assert_not_called()

    def test_environment_proxy_requested_but_not_configured_fails_clearly(self) -> None:
        client = BtbtlaClient(
            {
                "base_url": "https://www.btbtla.com",
                "use_env_proxy": True,
                "proxy_enabled": False,
            },
            {},
        )
        client.session.get = Mock()
        try:
            with patch("fnos_media_import.providers.btbtla.requests.utils.get_environ_proxies", return_value={}):
                result = client.probe_connection()
        finally:
            client.close()

        self.assertFalse(result["success"])
        self.assertEqual(result["mode"], "environment")
        self.assertTrue(result["proxy"]["requested"])
        self.assertFalse(result["proxy"]["configured"])
        self.assertFalse(result["proxy_applied"])
        client.session.get.assert_not_called()

    def test_missing_pysocks_dependency_returns_actionable_error(self) -> None:
        client = BtbtlaClient(
            {
                "base_url": "https://www.btbtla.com",
                "proxy_enabled": True,
                "proxy_url": "socks5h://proxy.local:1080",
            },
            {},
        )
        client.session.get = Mock(
            side_effect=requests.exceptions.InvalidSchema("Missing dependencies for SOCKS support."),
        )
        try:
            with patch("fnos_media_import.providers.btbtla.socket.create_connection", return_value=Mock()):
                result = client.probe_connection()
        finally:
            client.close()

        self.assertFalse(result["success"])
        self.assertFalse(result["proxy_applied"])
        self.assertIn("PySocks", result["message"])
        self.assertIn("PySocks", result["target"]["error"])

    def test_invalid_proxy_ports_are_rejected(self) -> None:
        for proxy_url in ("socks5h://proxy.local:not-a-port", "socks5h://proxy.local:70000"):
            with self.subTest(proxy_url=proxy_url):
                with self.assertRaisesRegex(ValueError, "1-65535"):
                    BtbtlaClient(
                        {
                            "proxy_enabled": True,
                            "proxy_url": proxy_url,
                        },
                        {},
                    )

    def test_advanced_config_save_rejects_invalid_proxy_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "1-65535"):
            normalize_advanced_config_payload(
                {
                    "config": {
                        "btbtla": {
                            "proxy_enabled": True,
                            "proxy_url": "socks5h://proxy.local:70000",
                        }
                    }
                }
            )


class BtbtlaProxyDiagnosticsServiceTests(unittest.TestCase):
    def test_blank_form_proxy_reuses_saved_proxy_url(self) -> None:
        fake_client = Mock()
        fake_client.probe_connection.return_value = {
            "success": True,
            "message": "ok",
            "proxy_applied": True,
        }
        service = BtbtlaProxyDiagnosticsService(
            current_config=lambda: {
                "base_url": "https://www.btbtla.com",
                "proxy_enabled": True,
                "proxy_url": "socks5h://saved-user:saved-pass@proxy.local:1080",
            },
            current_routes=lambda: {"magnet": {"enabled": True}},
        )

        with patch(
            "fnos_media_import.services.btbtla_proxy_diagnostics_service.BtbtlaClient",
            return_value=fake_client,
        ) as client_class:
            result = service.test(
                {
                    "btbtla": {
                        "proxy_enabled": True,
                        "proxy_url": "",
                        "timeout": 20,
                    }
                }
            )

        candidate, routes = client_class.call_args.args
        self.assertEqual(candidate["proxy_url"], "socks5h://saved-user:saved-pass@proxy.local:1080")
        self.assertEqual(candidate["timeout"], 20)
        self.assertEqual(routes, {"magnet": {"enabled": True}})
        self.assertTrue(result["tested_unsaved_values"])
        fake_client.close.assert_called_once()

    def test_retry_form_values_are_forwarded_to_temporary_client(self) -> None:
        fake_client = Mock()
        fake_client.probe_connection.return_value = {"success": True, "message": "ok"}
        service = BtbtlaProxyDiagnosticsService(
            current_config=lambda: {"request_retries": 2, "retry_delay_seconds": 0.4},
            current_routes=lambda: {},
        )

        with patch(
            "fnos_media_import.services.btbtla_proxy_diagnostics_service.BtbtlaClient",
            return_value=fake_client,
        ) as client_class:
            service.test(
                {
                    "btbtla": {
                        "request_retries": 4,
                        "retry_delay_seconds": 0.2,
                    }
                }
            )

        candidate, _routes = client_class.call_args.args
        self.assertEqual(candidate["request_retries"], 4)
        self.assertEqual(candidate["retry_delay_seconds"], 0.2)

    def test_service_error_does_not_leak_proxy_credentials(self) -> None:
        service = BtbtlaProxyDiagnosticsService(
            current_config=lambda: {},
            current_routes=lambda: {},
        )
        with patch(
            "fnos_media_import.services.btbtla_proxy_diagnostics_service.BtbtlaClient",
            side_effect=RuntimeError("failed socks5h://secret-user:secret-pass@proxy.local:1080"),
        ):
            result = service.test({"btbtla": {"proxy_enabled": True}})

        self.assertFalse(result["success"])
        self.assertNotIn("secret-user", result["message"])
        self.assertNotIn("secret-pass", result["message"])
        self.assertIn("socks5h://***@", result["message"])


if __name__ == "__main__":
    unittest.main()
