from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.services.public_search_service import PublicSearchDependencies, PublicSearchService


class SearchCacheRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Database(Path(temp.name) / "search-cache.db")
        self.database.init_schema()
        self.repos = RepositorySet.from_database(self.database)
        self.clock = {"now": "2030-01-01T00:00:00Z", "expiry": "2030-01-01T01:00:00Z"}
        self.item = {
            "url": "https://example.invalid/share/cache-refresh",
            "source_type": "quark",
            "title": "Original title",
            "password": "old-code",
            "size": 123,
            "raw_data": {"revision": 1},
        }
        for module, name, callback in (
            ("search_cache_command_repository", "_utc_now", lambda: self.clock["now"]),
            ("search_cache_query_repository", "_utc_now", lambda: self.clock["now"]),
            ("search_cache_command_repository", "_utc_minutes_from_now", lambda _m: self.clock["expiry"]),
        ):
            patcher = patch(f"fnos_media_import.repositories.{module}.{name}", side_effect=callback)
            patcher.start()
            self.addCleanup(patcher.stop)
        issued_ids = iter(("public-1", "public-2"))
        self.service = PublicSearchService(PublicSearchDependencies(
            search=SimpleNamespace(search=lambda *_args, **_kwargs: {"items": [dict(self.item)]}),
            cache_queries=self.repos.search_cache_queries,
            cache_commands=self.repos.search_cache_commands,
            new_public_id=lambda: next(issued_ids),
            present_item=lambda item, **kwargs: {"public_id": kwargs["public_id"], "title": item["title"]},
            log_info=lambda *_args: None,
        ))

    def search(self) -> dict:
        return self.service.search(
            "movie", sources=["pansou"], token="", options={}, hide_full_links=True, trace_id="test",
        )

    def test_reused_id_refreshes_contents_and_full_expiry_without_new_row(self) -> None:
        first = self.search()
        original = self.repos.search_cache_queries.get_active("public-1")
        self.clock.update(now="2030-01-01T00:59:50Z", expiry="2030-01-01T01:59:50Z")
        self.item.update(title="Updated title", password="new-code", size=456, raw_data={"revision": 2})

        second = self.search()
        current = self.repos.search_cache_queries.get_active("public-1")

        self.assertEqual(first["items"][0]["public_id"], second["items"][0]["public_id"])
        self.assertEqual(second["expires_in_minutes"], 60)
        self.assertEqual(current["id"], original["id"])
        self.assertEqual(current["created_at"], original["created_at"])
        self.assertEqual(current["expires_at"], self.clock["expiry"])
        self.assertEqual(current["title"], second["items"][0]["title"])
        self.assertEqual(current["password"], "new-code")
        self.assertEqual(current["size"], "456")
        self.assertEqual(current["raw_data"], {"revision": 2})
        self.clock["now"] = "2030-01-01T01:00:01Z"
        self.assertIsNotNone(self.repos.search_cache_queries.get_active("public-1"))
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0], 1)

    def test_row_expiring_between_lookup_and_refresh_is_recreated_with_same_public_id(self) -> None:
        self.search()
        self.clock.update(now="2030-01-01T00:59:59Z", expiry="2030-01-01T02:00:01Z")
        find = self.repos.search_cache_queries.find_active_by_urls

        def expire_after_lookup(*args):
            result = find(*args)
            self.clock["now"] = "2030-01-01T01:00:01Z"
            self.repos.search_cache_commands.prune_expired()
            return result

        with patch.object(self.repos.search_cache_queries, "find_active_by_urls", side_effect=expire_after_lookup):
            result = self.search()

        self.assertEqual(result["items"][0]["public_id"], "public-1")
        current = self.repos.search_cache_queries.get_active("public-1")
        self.assertIsNotNone(current)
        self.assertEqual(current["expires_at"], self.clock["expiry"])

    def test_refresh_failure_does_not_return_an_unpersisted_result(self) -> None:
        self.search()
        self.item["title"] = "New title"
        with patch.object(self.repos.search_cache_commands, "save_many", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                self.search()
        self.assertEqual(self.repos.search_cache_queries.get_active("public-1")["title"], "Original title")

    def test_invalid_legacy_raw_json_does_not_break_cache_lookup(self) -> None:
        self.search()
        with self.database.connect() as connection:
            connection.execute("UPDATE search_cache SET raw_data = ? WHERE public_id = ?", ("{invalid", "public-1"))
        self.assertEqual(self.repos.search_cache_queries.get_active("public-1")["raw_data"], {})


if __name__ == "__main__":
    unittest.main()
