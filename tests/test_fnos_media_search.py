from __future__ import annotations

import unittest

from fnos_media_import.media.fnos import API_SEARCH_LIST, FnosMediaRefresher, _media_search_item

# Trimmed from a live `/v/api/v1/search/list?q=繁花` response.
LIVE_TV_ROW = {
    "guid": "31c4d6e337644ce982f88d8a15f12806",
    "lan": "zh-CN",
    "imdb_id": "tt3881914",
    "trim_id": "tt106841",
    "title": "繁花",
    "type": "TV",
    "poster": "/02/10/RXFg9YOlYYTNwMynBkZifbn3VpVnzd401lk1CjS099E0CKLry11jo1SWprxN3xIvYnvC0rNjhqZt71cA8Rr0uDwyKV.webp",
    "vote_average": "8.6",
    "release_date": "2023-12-27",
    "first_air_date": "2023-12-27",
    "last_air_date": "2024-01-09",
    "season_number": 0,
    "episode_number": 0,
    "number_of_seasons": 1,
    "number_of_episodes": 30,
    "local_number_of_seasons": 2,
    "local_number_of_episodes": 30,
    "status": "Ended",
    "overview": "九十年代的上海处处是机遇与希望。",
    "ancestor_guid": "e6e16f23d8384a428dd81bbb11fc4147",
    "ancestor_name": "电视剧",
    "ancestor_category": "TV",
    "watched": 0,
    "is_favorite": 0,
    "duration": 0,
    "file_name": "",
}

LIVE_EPISODE_PLACEHOLDER = {
    "guid": "3277b362b73a48b795900b0c3b2f0d16",
    "tv_title": "繁花",
    "title": "",
    "type": "Episode",
    "parent_guid": "3f4d37f86c58445abda2aee070905680",
    "parent_title": "特别篇",
    "season_number": 0,
    "episode_number": -1,
    "number_of_episodes": 0,
    "local_number_of_seasons": 0,
    "local_number_of_episodes": 0,
    "ancestor_name": "电视剧",
    "ancestor_category": "TV",
}


class MediaSearchItemTests(unittest.TestCase):
    def test_tv_row_with_local_episodes_is_imported(self) -> None:
        item = _media_search_item(LIVE_TV_ROW)
        self.assertEqual(item["title"], "繁花")
        self.assertEqual(item["type"], "TV")
        self.assertEqual(item["year"], "2023")
        self.assertEqual(item["library"], "电视剧")
        self.assertEqual(item["local_episodes"], 30)
        self.assertEqual(item["total_episodes"], 30)
        self.assertTrue(item["in_library"])

    def test_placeholder_episode_is_not_imported(self) -> None:
        item = _media_search_item(LIVE_EPISODE_PLACEHOLDER)
        self.assertEqual(item["title"], "繁花")
        self.assertEqual(item["parent_title"], "特别篇")
        self.assertEqual(item["local_episodes"], 0)
        self.assertFalse(item["in_library"])

    def test_movie_row_counts_as_imported(self) -> None:
        item = _media_search_item({"title": "沙丘2", "type": "Movie", "release_date": "2024-03-01"})
        self.assertTrue(item["in_library"])
        self.assertEqual(item["year"], "2024")

    def test_unknown_type_without_files_is_not_imported(self) -> None:
        item = _media_search_item({"title": "未知", "type": ""})
        self.assertFalse(item["in_library"])

    def test_long_overview_is_truncated(self) -> None:
        item = _media_search_item({"title": "x", "type": "Movie", "overview": "字" * 900})
        self.assertLessEqual(len(item["overview"]), 400)


class _StubRefresher(FnosMediaRefresher):
    """Client with a pre-set token and a canned transport response."""

    def __init__(self, response: dict) -> None:
        super().__init__({"server_url": "http://nas.invalid:5666", "username": "u", "password": "p"})
        self.token = "stub-token"
        self._response = response
        self.calls: list[tuple[str, str, dict | None]] = []

    def _request(self, method, path, *, data=None, params=None, require_auth=True):  # noqa: ANN001
        self.calls.append((method, path, params))
        return self._response


class MediaSearchClientTests(unittest.TestCase):
    def test_uses_search_list_endpoint_with_q(self) -> None:
        client = _StubRefresher({"code": 0, "msg": "", "data": [LIVE_TV_ROW]})
        result = client.search_media("繁花", limit=5)
        self.assertTrue(result["success"])
        self.assertEqual(client.calls, [("GET", API_SEARCH_LIST, {"q": "繁花"})])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "繁花")
        self.assertTrue(result["items"][0]["in_library"])

    def test_limit_caps_items(self) -> None:
        rows = [dict(LIVE_TV_ROW, guid=str(index), title=f"剧{index}") for index in range(10)]
        client = _StubRefresher({"code": 0, "data": rows})
        result = client.search_media("剧", limit=3)
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["total"], 10)

    def test_empty_keyword_short_circuits(self) -> None:
        client = _StubRefresher({"code": 0, "data": []})
        result = client.search_media("   ")
        self.assertFalse(result["success"])
        self.assertEqual(client.calls, [])

    def test_error_envelope_is_reported(self) -> None:
        client = _StubRefresher({"code": 1, "msg": "token invalid", "data": []})
        result = client.search_media("繁花")
        self.assertFalse(result["success"])
        self.assertIn("token invalid", result["message"])

    def test_unconfigured_client_fails_cleanly(self) -> None:
        client = FnosMediaRefresher({})
        result = client.search_media("繁花")
        self.assertFalse(result["success"])
        self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()
