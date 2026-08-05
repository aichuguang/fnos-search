from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from fnos_media_import.app import (
    _create_subscription_from_hot_candidate,
    _find_hot_existing_subscription,
    _hot_identity_matches,
)
from fnos_media_import.services.trending_discovery_service import TrendingDiscoveryService


class _TmdbStub:
    def __init__(self, rows: list[dict[str, Any]] | None = None, configured: bool = True, error: Exception | None = None) -> None:
        self._rows = rows or []
        self.configured = configured
        self._error = error

    def search(self, _query: str, _media_type: str) -> list[dict[str, Any]]:
        if self._error is not None:
            raise self._error
        return list(self._rows)


class HotCandidateSubscribeTests(unittest.TestCase):
    def test_existing_subscription_requires_compatible_year(self) -> None:
        item = {"title": "同名作品", "media_type": "tv", "year": "2024"}
        subscriptions = [
            {"id": 1, "title": "同名作品", "category": "tv", "year": "2023"},
            {"id": 2, "title": "同名作品", "category": "tv", "year": "2024"},
        ]

        matched = _find_hot_existing_subscription(item, subscriptions)

        self.assertIsNotNone(matched)
        self.assertEqual(matched["id"], 2)

    def test_existing_subscription_rejects_same_title_ambiguity_without_year(self) -> None:
        item = {"title": "同名作品", "media_type": "tv"}
        subscriptions = [
            {"id": 1, "title": "同名作品", "category": "tv", "year": "2023"},
            {"id": 2, "title": "同名作品", "category": "tv", "year": "2024"},
        ]

        self.assertIsNone(_find_hot_existing_subscription(item, subscriptions))

    def test_existing_job_requires_compatible_category_and_year(self) -> None:
        item = {"title": "同名作品", "media_type": "movie", "year": "2024"}

        self.assertFalse(
            _hot_identity_matches(item, {"title": "同名作品", "category": "tv", "year": "2024"})
        )
        self.assertFalse(
            _hot_identity_matches(item, {"title": "同名作品", "category": "movie", "year": "2023"})
        )
        self.assertFalse(
            _hot_identity_matches(item, {"title": "同名作品", "category": "movie"})
        )
        self.assertTrue(
            _hot_identity_matches(item, {"title": "同名作品", "category": "movie", "year": "2024"})
        )

    def test_same_tmdb_identity_can_tolerate_missing_year_metadata(self) -> None:
        item = {"title": "标题 A", "media_type": "tv", "year": "2024", "tmdb_id": 88}
        existing = {"title": "标题 B", "category": "tv", "tmdb_id": 88}

        self.assertTrue(_hot_identity_matches(item, existing))

    def test_same_tmdb_identity_still_requires_exact_season_identity(self) -> None:
        unknown = {"title": "标题 A", "media_type": "tv", "tmdb_id": 88}
        specials = {"title": "标题 A", "category": "tv", "tmdb_id": 88, "season": 0}

        self.assertFalse(_hot_identity_matches(unknown, specials))
        self.assertFalse(_hot_identity_matches(specials, unknown))
        self.assertTrue(_hot_identity_matches(specials, {**specials, "title": "标题 B"}))

    def test_candidate_merge_keeps_same_title_different_years_separate(self) -> None:
        snapshots = [
            {
                "source": "source-a",
                "source_id": "a-2023",
                "title": "同名作品",
                "normalized_title": "同名作品",
                "year": 2023,
                "media_type": "tv",
                "rank": 1,
            },
            {
                "source": "source-b",
                "source_id": "b-2024",
                "title": "同名作品",
                "normalized_title": "同名作品",
                "year": 2024,
                "media_type": "tv",
                "rank": 2,
            },
        ]

        merged = TrendingDiscoveryService._merge_candidates(snapshots)

        self.assertEqual(len(merged), 2)
        self.assertEqual({item["year"] for item in merged}, {2023, 2024})
        self.assertEqual(len({item["canonical_key"] for item in merged}), 2)

    def test_candidate_without_year_joins_only_unambiguous_known_year(self) -> None:
        snapshots = [
            {
                "source": "source-a",
                "source_id": "a-2024",
                "title": "唯一作品",
                "normalized_title": "唯一作品",
                "year": 2024,
                "media_type": "tv",
                "rank": 1,
            },
            {
                "source": "source-b",
                "source_id": "b-unknown",
                "title": "唯一作品",
                "normalized_title": "唯一作品",
                "year": None,
                "media_type": "tv",
                "rank": 2,
            },
        ]

        merged = TrendingDiscoveryService._merge_candidates(snapshots)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["year"], 2024)
        self.assertEqual(merged[0]["canonical_key"], "唯一作品|tv|2024")
        self.assertEqual(len(merged[0]["sources"]), 2)

    def test_candidate_without_any_known_year_uses_legacy_key_without_trailing_separator(self) -> None:
        merged = TrendingDiscoveryService._merge_candidates(
            [
                {
                    "source": "source-a",
                    "source_id": "unknown",
                    "title": "年份未知作品",
                    "normalized_title": "年份未知作品",
                    "year": None,
                    "media_type": "tv",
                    "rank": 1,
                }
            ]
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["canonical_key"], "年份未知作品|tv")
        self.assertEqual(merged[0]["legacy_canonical_key"], "年份未知作品|tv")

    def test_stale_item_subscription_id_is_not_trusted_without_tmdb_validation(self) -> None:
        item = {"id": 1, "title": "仙逆", "media_type": "tv", "subscription_id": 88}
        result, status = _create_subscription_from_hot_candidate(
            item,
            _TmdbStub(configured=False),
            create_subscription=lambda _payload: {},
        )
        self.assertEqual(status, 400)
        self.assertFalse(result["success"])

    def test_archived_subscription_is_not_reused_by_title_discovery(self) -> None:
        item = {"title": "仙逆", "media_type": "anime", "year": "2023"}
        subscriptions = [
            {"id": 1, "title": "仙逆", "category": "anime", "year": "2023", "status": "archived"}
        ]

        self.assertIsNone(_find_hot_existing_subscription(item, subscriptions))

    def test_missing_title_rejected(self) -> None:
        item = {"id": 1, "title": "", "media_type": "tv"}
        result, status = _create_subscription_from_hot_candidate(item, _TmdbStub(), create_subscription=lambda _p: {})
        self.assertEqual(status, 400)
        self.assertFalse(result["success"])

    def test_tmdb_not_configured(self) -> None:
        item = {"id": 1, "title": "仙逆", "media_type": "tv"}
        result, status = _create_subscription_from_hot_candidate(item, _TmdbStub(configured=False), create_subscription=lambda _p: {})
        self.assertEqual(status, 400)
        self.assertIn("TMDB 未配置", result["message"])

    def test_tmdb_search_error(self) -> None:
        item = {"id": 1, "title": "仙逆", "media_type": "tv"}
        result, status = _create_subscription_from_hot_candidate(item, _TmdbStub(error=RuntimeError("boom")), create_subscription=lambda _p: {})
        self.assertEqual(status, 502)

    def test_tmdb_no_match(self) -> None:
        item = {"id": 1, "title": "仙逆", "media_type": "tv"}
        result, status = _create_subscription_from_hot_candidate(item, _TmdbStub(rows=[]), create_subscription=lambda _p: {})
        self.assertEqual(status, 400)
        self.assertIn("未匹配", result["message"])

    def test_creates_subscription_with_correct_payload(self) -> None:
        item = {"id": 1, "title": "仙逆", "media_type": "tv", "year": "2023"}
        captured: list[dict[str, Any]] = []
        tmdb = _TmdbStub(rows=[{"id": 100, "title": "仙逆 (2023)", "media_type": "tv", "year": "2023"}])

        def fake_create(payload: dict[str, Any]) -> dict[str, Any]:
            captured.append(payload)
            return {"id": 42, "title": "仙逆 (2023)"}

        result, status = _create_subscription_from_hot_candidate(item, tmdb, fake_create)
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])
        self.assertEqual(result["subscription_id"], 42)
        self.assertEqual(captured[0]["tmdb_id"], 100)
        self.assertEqual(captured[0]["media_type"], "tv")
        self.assertEqual(captured[0]["category"], "tv")
        self.assertEqual(captured[0]["year"], "2023")

    def test_specials_candidate_preserves_season_zero_in_subscription_payload(self) -> None:
        captured: list[dict[str, Any]] = []
        item = {"id": 1, "title": "特别篇", "media_type": "tv", "year": "2024", "season": 0}
        tmdb = _TmdbStub(rows=[{"id": 100, "title": "特别篇", "media_type": "tv", "year": "2024"}])

        result, status = _create_subscription_from_hot_candidate(
            item,
            tmdb,
            lambda payload: captured.append(payload) or {"id": 42},
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["success"])
        self.assertEqual(captured[0]["season"], 0)

    def test_exact_title_wins_over_unrelated_same_year_result(self) -> None:
        item = {"id": 1, "title": "仙逆", "media_type": "tv", "year": "2023"}
        captured: list[dict[str, Any]] = []
        tmdb = _TmdbStub(
            rows=[
                {"id": 1, "title": "完全不同的作品", "media_type": "tv", "year": "2023"},
                {"id": 2, "title": "仙逆", "original_title": "仙逆", "media_type": "tv", "year": "2023"},
            ]
        )

        result, status = _create_subscription_from_hot_candidate(
            item,
            tmdb,
            lambda payload: captured.append(payload) or {"id": 22},
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["subscription_id"], 22)
        self.assertEqual(captured[0]["tmdb_id"], 2)

    def test_original_title_can_supply_high_confidence_match(self) -> None:
        item = {"id": 1, "title": "The Long Season", "original_title": "漫长的季节", "media_type": "tv", "year": "2023"}
        captured: list[dict[str, Any]] = []
        tmdb = _TmdbStub(
            rows=[{"id": 10, "title": "漫长的季节", "original_title": "The Long Season", "media_type": "tv", "year": "2023"}]
        )
        result, status = _create_subscription_from_hot_candidate(
            item,
            tmdb,
            lambda payload: captured.append(payload) or {"id": 23},
        )
        self.assertEqual(status, 200)
        self.assertEqual(captured[0]["tmdb_id"], 10)
        self.assertIn("The Long Season", captured[0]["aliases"])

    def test_low_confidence_match_is_rejected(self) -> None:
        item = {"id": 1, "title": "仙逆", "media_type": "tv", "year": "2023"}
        result, status = _create_subscription_from_hot_candidate(
            item,
            _TmdbStub(rows=[{"id": 1, "title": "完全不同的作品", "media_type": "tv", "year": "2023"}]),
            lambda _payload: {"id": 99},
        )
        self.assertEqual(status, 409)
        self.assertFalse(result["success"])
        self.assertIn("置信度不足", result["message"])
        self.assertEqual(result["matches"][0]["tmdb_id"], 1)

    def test_exact_title_without_candidate_year_is_not_auto_bound(self) -> None:
        item = {"id": 1, "title": "同名翻拍", "media_type": "tv"}
        result, status = _create_subscription_from_hot_candidate(
            item,
            _TmdbStub(rows=[{"id": 1, "title": "同名翻拍", "media_type": "tv", "year": "2024"}]),
            lambda _payload: {"id": 99},
        )
        self.assertEqual(status, 409)
        self.assertFalse(result["success"])
        self.assertIn("置信度不足", result["message"])

    def test_ambiguous_high_confidence_matches_are_rejected(self) -> None:
        item = {"id": 1, "title": "重启人生", "media_type": "tv", "year": "2023"}
        rows = [
            {"id": 10, "title": "重启人生", "media_type": "tv", "year": "2023"},
            {"id": 11, "title": "重启人生", "media_type": "tv", "year": "2023"},
        ]
        result, status = _create_subscription_from_hot_candidate(item, _TmdbStub(rows=rows), lambda _payload: {"id": 99})
        self.assertEqual(status, 409)
        self.assertFalse(result["success"])
        self.assertIn("歧义", result["message"])

    def test_movie_maps_category_to_movie(self) -> None:
        item = {"id": 2, "title": "流浪地球2", "media_type": "movie", "year": "2023"}
        captured: list[dict[str, Any]] = []
        tmdb = _TmdbStub(rows=[{"id": 7, "title": "流浪地球2", "media_type": "movie", "year": "2023"}])

        def fake_create(payload: dict[str, Any]) -> dict[str, Any]:
            captured.append(payload)
            return {"id": 9}

        result, status = _create_subscription_from_hot_candidate(item, tmdb, fake_create)
        self.assertEqual(status, 200)
        self.assertEqual(captured[0]["media_type"], "movie")
        self.assertEqual(captured[0]["category"], "movie")

    def test_anime_maps_category_to_anime_and_searches_tv(self) -> None:
        item = {"id": 3, "title": "仙逆", "media_type": "anime", "year": "2023"}
        captured: list[dict[str, Any]] = []
        searched: list[tuple[str, str]] = []

        class _RecordingTmdb(_TmdbStub):
            def search(self, query: str, media_type: str) -> list[dict[str, Any]]:
                searched.append((query, media_type))
                return [{"id": 100, "title": "仙逆", "media_type": "tv", "year": "2023"}]

        def fake_create(payload: dict[str, Any]) -> dict[str, Any]:
            captured.append(payload)
            return {"id": 42}

        result, status = _create_subscription_from_hot_candidate(item, _RecordingTmdb(), fake_create)
        self.assertEqual(status, 200)
        # TMDB 搜索用 tv 类型，分类归 anime
        self.assertEqual(searched[0][1], "tv")
        self.assertEqual(captured[0]["category"], "anime")
        self.assertEqual(captured[0]["media_type"], "tv")

    def test_variety_maps_category_to_variety(self) -> None:
        item = {"id": 4, "title": "某某综艺", "media_type": "variety", "year": "2024"}
        captured: list[dict[str, Any]] = []
        tmdb = _TmdbStub(rows=[{"id": 50, "title": "某某综艺", "media_type": "tv", "year": "2024"}])

        def fake_create(payload: dict[str, Any]) -> dict[str, Any]:
            captured.append(payload)
            return {"id": 11}

        result, status = _create_subscription_from_hot_candidate(item, tmdb, fake_create)
        self.assertEqual(status, 200)
        self.assertEqual(captured[0]["category"], "variety")
        self.assertEqual(captured[0]["media_type"], "tv")

    def test_create_subscription_value_error_propagated(self) -> None:
        item = {"id": 1, "title": "仙逆", "media_type": "tv", "year": "2023"}
        tmdb = _TmdbStub(rows=[{"id": 100, "title": "仙逆", "media_type": "tv", "year": "2023"}])

        def fail_create(_payload: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("请先从 TMDB 搜索并选择要追更的影视")

        result, status = _create_subscription_from_hot_candidate(item, tmdb, fail_create)
        self.assertEqual(status, 400)
        self.assertFalse(result["success"])

    def test_existing_identity_is_reported_as_bound_not_created(self) -> None:
        item = {"id": 1, "title": "仙逆", "media_type": "tv", "year": "2023"}
        tmdb = _TmdbStub(rows=[{"id": 100, "title": "仙逆", "media_type": "tv", "year": "2023"}])
        result, status = _create_subscription_from_hot_candidate(
            item,
            tmdb,
            lambda _payload: {"id": 42, "_created": False},
        )
        self.assertEqual(status, 200)
        self.assertFalse(result["created"])
        self.assertIn("绑定现有", result["message"])


if __name__ == "__main__":
    unittest.main()
