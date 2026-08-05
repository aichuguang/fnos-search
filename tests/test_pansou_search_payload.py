from __future__ import annotations

import unittest

from fnos_media_import.providers.pansou import PanSouClient


class PanSouSearchPayloadTests(unittest.TestCase):
    def test_provider_selector_does_not_become_a_pansou_plugin(self) -> None:
        client = PanSouClient(
            {
                "base_url": "http://127.0.0.1:8055",
                "src": "all",
                "plugins": [],
            },
            {},
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
            {},
        )

        payload = client._search_payload("仙逆", ["pansou"], refresh=False)

        self.assertEqual(payload["src"], "all")
        self.assertEqual(payload["plugins"], ["ash", "quark4k"])


if __name__ == "__main__":
    unittest.main()
