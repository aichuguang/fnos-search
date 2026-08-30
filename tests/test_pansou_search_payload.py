from __future__ import annotations

import unittest

from fnos_media_import.providers.pansou import PanSouClient
from fnos_media_import.public_web import _public_request_response, _public_result_provider_badges


class PanSouSearchPayloadTests(unittest.TestCase):
    def test_default_concurrency_is_delegated_to_pansou(self) -> None:
        """A pinned ``conc`` makes cold searches time out upstream.

        Measured against a live PanSou instance, the same keyword returned
        HTTP 504 after 60s with ``conc=10`` and HTTP 200 in ~4s when ``conc``
        was omitted, because a fixed concurrency makes instances with many
        plugins fan out in batches.  ``0`` means "use PanSou's own setting".
        """

        client = PanSouClient({"base_url": "http://127.0.0.1:8055"})

        payload = client._search_payload("仙逆", ["pansou"], refresh=False)

        self.assertEqual(client.conc, 0)
        self.assertNotIn("conc", payload)

    def test_explicit_concurrency_is_preserved(self) -> None:
        client = PanSouClient({"base_url": "http://127.0.0.1:8055", "conc": 10})

        payload = client._search_payload("仙逆", ["pansou"], refresh=False)

        self.assertEqual(payload["conc"], 10)

    def test_shipped_config_default_does_not_pin_concurrency(self) -> None:
        """The client default is useless if ``config.py`` always sends ``conc``."""

        from fnos_media_import.config import load_config

        pansou_config = load_config().raw.get("pansou", {})
        self.assertEqual(pansou_config.get("conc"), 0)

        client = PanSouClient(pansou_config)
        self.assertNotIn(
            "conc", client._search_payload("仙逆", ["pansou"], refresh=False)
        )

    def test_result_provider_priority_is_complete_and_stable(self) -> None:
        client = PanSouClient(
            {
                "base_url": "http://127.0.0.1:8055",
                "result_provider_priority": ["mobile", "mobile", "invalid"],
            }
        )

        self.assertEqual(
            client.result_provider_priority,
            ["mobile", "quark", "tianyi", "magnet"],
        )

    def test_shipped_config_has_result_provider_priority_default(self) -> None:
        from fnos_media_import.config import load_config

        pansou_config = load_config().raw.get("pansou", {})

        self.assertEqual(
            pansou_config.get("result_provider_priority"),
            ["quark", "mobile", "tianyi", "magnet"],
        )

    def test_shipped_config_has_disabled_provider_badges_by_default(self) -> None:
        from fnos_media_import.config import load_config

        badges = load_config().raw.get("pansou", {}).get("result_provider_badges")

        self.assertEqual(badges["mobile"], {"enabled": False, "text": "秒入库"})
        self.assertEqual(badges["magnet"], {"enabled": False, "text": "快速入库"})

    def test_no_enabled_cloud_types_skips_pansou_request(self) -> None:
        client = PanSouClient(
            {
                "base_url": "http://127.0.0.1:8055",
                "cloud_types": [],
            }
        )

        result = client.search("测试资源")

        self.assertEqual(client.cloud_types, [])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["raw"]["snapshots"], [])

    def test_public_badge_config_exposes_only_display_fields(self) -> None:
        badges = _public_result_provider_badges(
            {
                "result_provider_badges": {
                    "mobile": {"enabled": True, "text": "秒入库", "private": "hidden"},
                    "magnet": {"enabled": True, "text": "快速入库"},
                },
                "default_token": "secret",
            }
        )

        self.assertEqual(badges["mobile"], {"enabled": True, "text": "秒入库"})
        self.assertEqual(badges["magnet"], {"enabled": True, "text": "快速入库"})
        self.assertNotIn("private", badges["mobile"])

    def test_public_request_response_still_returns_media_identity(self) -> None:
        response = _public_request_response(
            {
                "request_token": "request-1",
                "title": "测试影片",
                "category": "movie",
                "status": "pending",
                "raw_data": {
                    "media_identity": {"tmdb_id": 123, "media_type": "movie"},
                    "job_result": {"destination_id": "movie-main"},
                },
            }
        )

        self.assertEqual(response["token"], "request-1")
        self.assertEqual(response["destination_id"], "movie-main")
        self.assertEqual(response["media_identity"]["tmdb_id"], 123)

    def test_provider_selector_does_not_become_a_pansou_plugin(self) -> None:
        client = PanSouClient(
            {
                "base_url": "http://127.0.0.1:8055",
                "src": "all",
                "plugins": [],
            },
        )

        payload = client._search_payload("仙逆", ["pansou"], refresh=False)

        self.assertEqual(payload["src"], "all")
        self.assertNotIn("plugins", payload)

    def test_explicitly_configured_plugins_are_preserved(self) -> None:
        client = PanSouClient(
            {
                "base_url": "http://127.0.0.1:8055",
                "src": "all",
                "plugins": ["ash", "quark4k"],
            },
        )

        payload = client._search_payload("仙逆", ["pansou"], refresh=False)

        self.assertEqual(payload["src"], "all")
        self.assertEqual(payload["plugins"], ["ash", "quark4k"])

    def test_http_stability_config_is_applied(self) -> None:
        client = PanSouClient(
            {
                "base_url": "http://127.0.0.1:8055",
                "timeout": 20,
                "connect_timeout": 2.5,
                "read_timeout": 45,
                "request_retries": 3,
                "retry_delay_seconds": 0.4,
            }
        )

        self.assertEqual(client.connect_timeout, 2.5)
        self.assertEqual(client.read_timeout, 45)
        self.assertEqual(client.timeout, 45)
        self.assertEqual(client.request_retries, 3)
        self.assertEqual(client.retry_delay_seconds, 0.4)

    def test_legacy_timeout_remains_the_default_read_timeout(self) -> None:
        client = PanSouClient(
            {
                "base_url": "http://127.0.0.1:8055",
                "timeout": 61,
            }
        )

        self.assertEqual(client.read_timeout, 61)
        self.assertEqual(client.timeout, 61)


if __name__ == "__main__":
    unittest.main()
