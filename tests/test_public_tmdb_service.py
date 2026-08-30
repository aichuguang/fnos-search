from __future__ import annotations

import unittest

from fnos_media_import.services.public_tmdb_service import PublicTmdbService


class _Client:
    configured = True

    def search(self, query, media_type):
        return [{
            "id": 101,
            "media_type": "tv",
            "title": query,
            "original_title": "Original",
            "year": "2024",
            "overview": "Overview",
            "poster_path": "https://image.test/poster.jpg",
        }]


class _CountingClient(_Client):
    def __init__(self, rows=None) -> None:
        self.calls = 0
        self._rows = rows

    def search(self, query, media_type):
        self.calls += 1
        if self._rows is not None:
            return self._rows
        return super().search(query, media_type)


class PublicTmdbServiceTests(unittest.TestCase):
    def test_signed_selection_round_trip_and_tamper_rejection(self) -> None:
        service = PublicTmdbService(client=_Client(), secret_key="secret", now=lambda: 1000)
        result = service.search("测试")
        self.assertEqual(result["status"], "ready")
        candidate = result["items"][0]
        self.assertEqual(service.resolve_token(candidate["selection_token"])["tmdb_id"], 101)
        body, signature = candidate["selection_token"].rsplit(".", 1)
        self.assertIsNone(service.resolve_token(f"{body}x.{signature}"))

    def test_expired_selection_is_invalid(self) -> None:
        current = {"value": 1000}
        service = PublicTmdbService(client=_Client(), secret_key="secret", now=lambda: current["value"])
        token = service.search("测试")["items"][0]["selection_token"]
        current["value"] += service.TOKEN_TTL_SECONDS + 1
        self.assertIsNone(service.resolve_token(token))

    def test_repeated_search_hits_cache_and_reissues_token(self) -> None:
        client = _CountingClient()
        service = PublicTmdbService(client=client, secret_key="secret", now=lambda: 1000)
        first = service.search("测试")
        second = service.search("测试")
        self.assertEqual(client.calls, 1)
        self.assertEqual(first["status"], second["status"])
        self.assertEqual(service.resolve_token(second["items"][0]["selection_token"])["tmdb_id"], 101)

    def test_cache_expires_after_ttl(self) -> None:
        current = {"value": 1000.0}
        client = _CountingClient()
        service = PublicTmdbService(client=client, secret_key="secret", now=lambda: current["value"])
        service.search("测试")
        current["value"] += service.SEARCH_CACHE_TTL_SECONDS + 1
        service.search("测试")
        self.assertEqual(client.calls, 2)

    def test_empty_results_are_not_cached(self) -> None:
        client = _CountingClient(rows=[])
        service = PublicTmdbService(client=client, secret_key="secret", now=lambda: 1000)
        self.assertEqual(service.search("测试")["status"], "empty")
        self.assertEqual(service.search("测试")["status"], "empty")
        self.assertEqual(client.calls, 2)

    def test_cache_capacity_evicts_oldest(self) -> None:
        client = _CountingClient()
        service = PublicTmdbService(client=client, secret_key="secret", now=lambda: 1000, cache_max_entries=1)
        service.search("甲")
        service.search("乙")
        service.search("甲")
        self.assertEqual(client.calls, 3)


if __name__ == "__main__":
    unittest.main()
