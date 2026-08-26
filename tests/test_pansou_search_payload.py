from __future__ import annotations

import unittest

from fnos_media_import.providers.pansou import PanSouClient


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


if __name__ == "__main__":
    unittest.main()
