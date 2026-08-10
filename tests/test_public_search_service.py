from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.search.aggregator import (
    SearchAggregator,
    SearchProvidersUnavailableError,
)
from fnos_media_import.services.public_search_service import (
    PublicSearchDependencies,
    PublicSearchService,
)
from fnos_media_import.services.public_bt_resolve_service import (
    PublicBtResolveService,
)
from fnos_media_import.services.public_manual_preview_service import (
    PublicManualPreviewService,
)
from fnos_media_import.services.public_sixpan_preview_service import (
    PublicSixpanPreviewService,
)


class _Search:
    @staticmethod
    def search(
        keyword: str,
        *,
        sources: list[str],
        token: str,
        options: dict,
    ) -> dict:
        return {
            "success": True,
            "items": [
                {
                    "title": f"{keyword}-result",
                    "source_type": sources[0],
                    "url": "https://example.test/share/1",
                    "password": token,
                    "raw_data": {"options": options},
                }
            ],
        }


class PublicSearchRepositoryWiringTests(unittest.TestCase):
    def test_all_provider_failures_use_publicly_mappable_exception(self) -> None:
        provider = SimpleNamespace(
            key="failing",
            name="Failing",
            priority=1,
            is_enabled=lambda: True,
            search=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TimeoutError("provider timeout")
            ),
        )

        with self.assertRaises(SearchProvidersUnavailableError):
            SearchAggregator([provider]).search("movie")

    def test_search_cache_uses_explicit_query_and_command_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "public-search.db")
            database.init_schema()
            repositories = RepositorySet.from_database(database)
            issued_ids = iter(("public-1", "public-2"))
            service = PublicSearchService(
                PublicSearchDependencies(
                    search=_Search(),
                    cache_queries=repositories.search_cache_queries,
                    cache_commands=repositories.search_cache_commands,
                    new_public_id=lambda: next(issued_ids),
                    present_item=lambda item, **kwargs: {
                        "title": item["title"],
                        "public_id": kwargs["public_id"],
                    },
                    log_info=lambda *_args: None,
                )
            )

            first = service.search(
                "movie",
                sources=["quark"],
                token="code",
                options={"page": 1},
                hide_full_links=True,
                trace_id="trace-1",
            )
            second = service.search(
                "movie",
                sources=["quark"],
                token="code",
                options={"page": 1},
                hide_full_links=True,
                trace_id="trace-2",
            )

            self.assertEqual(first["items"][0]["public_id"], "public-1")
            self.assertEqual(second["items"][0]["public_id"], "public-1")

    def test_preview_services_use_explicit_cache_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "public-preview.db")
            database.init_schema()
            repositories = RepositorySet.from_database(database)

            link = SimpleNamespace(
                url="https://example.test/share/1",
                password="code",
                resource_kind="cloud189",
                provider_kind="tianyi",
                source_form="share",
                search_source_id="tianyi",
                supported=True,
                reason="",
            )
            manual = PublicManualPreviewService(
                cache_queries=repositories.search_cache_queries,
                cache_commands=repositories.search_cache_commands,
                new_public_id=lambda: "manual-1",
                detect_link=lambda *_args, **_kwargs: link,
                resource_detail=lambda cached, **_kwargs: {
                    "public_id": cached["public_id"]
                },
                present_item=lambda item, **kwargs: {
                    "title": item["title"],
                    "public_id": kwargs["public_id"],
                },
                detail_capability=lambda *_args, **_kwargs: {"available": True},
                quark_importer=lambda: None,
                cloud139_importer=lambda: None,
                sixpan_importer=lambda: None,
                hide_full_links=lambda: True,
                tianyi_importer=lambda: None,
            )
            manual_result = manual.preview(
                url=link.url,
                password=link.password,
                title="Manual",
            )
            self.assertEqual(manual_result["public_id"], "manual-1")

            repositories.search_cache_commands.save(
                "magnet-1",
                "manual_link",
                {
                    "title": "Magnet",
                    "source_type": "magnet",
                    "url": "magnet:?xt=urn:btih:abc",
                    "raw_data": {"url": "magnet:?xt=urn:btih:abc"},
                },
            )
            sixpan_importer = SimpleNamespace(
                configured=True,
                parse_resource=lambda **_kwargs: {"files": [{"name": "a.mkv"}]},
            )
            sixpan = PublicSixpanPreviewService(
                cache_queries=repositories.search_cache_queries,
                importer=lambda: sixpan_importer,
                validate_url=lambda value: str(value),
                detect_link=lambda *_args, **_kwargs: SimpleNamespace(
                    provider_kind="sixpan",
                    source_form="magnet",
                    url="magnet:?xt=urn:btih:abc",
                    resource_kind="magnet",
                ),
                summarize=lambda data: {"items": data["files"]},
            )
            sixpan_result, sixpan_status = sixpan.preview(public_id="magnet-1")
            self.assertEqual(sixpan_status, 200)
            self.assertTrue(sixpan_result["success"])

            repositories.search_cache_commands.save(
                "bt-1",
                "bt",
                {
                    "title": "BT",
                    "source_type": "bt_detail",
                    "url": "https://example.test/detail/1",
                    "raw_data": {"btbtla_detail_url": "https://example.test/detail/1"},
                },
            )
            bt_client = SimpleNamespace(
                resolve_magnet=lambda *_args, **_kwargs: {
                    "magnet": "magnet:?xt=urn:btih:def",
                    "supported": True,
                    "tdown_url": "https://example.test/download/1",
                }
            )
            bt = PublicBtResolveService(
                cache_queries=repositories.search_cache_queries,
                cache_commands=repositories.search_cache_commands,
                btbtla=lambda: bt_client,
                present_item=lambda item, **_kwargs: item,
                resource_detail=lambda *_args, **_kwargs: {"available": True},
                quark_importer=lambda: None,
                cloud139_importer=lambda: None,
                sixpan_importer=lambda: None,
                hide_full_links=lambda: True,
            )
            bt_result, bt_status = bt.resolve(public_id="bt-1")
            self.assertEqual(bt_status, 200)
            self.assertTrue(bt_result["success"])


if __name__ == "__main__":
    unittest.main()
