from __future__ import annotations

import unittest

from fnos_media_import.public_web import _public_resource_item
from fnos_media_import.search.aggregator import (
    SearchAggregator,
    _keyword_token_hit_score,
    _ranking_score,
    _relevance_score,
)


class _StaticProvider:
    key = "pansou"
    name = "PanSou"
    priority = 10

    def __init__(self, items: list[dict]) -> None:
        self.items = items

    def is_enabled(self) -> bool:
        return True

    def describe(self) -> dict:
        return {"key": self.key, "name": self.name, "enabled": True}

    def search(self, *_args, **_kwargs) -> dict:
        return {"items": [dict(item) for item in self.items]}


class SearchTokenScoringTests(unittest.TestCase):
    def test_multi_word_query_keeps_individual_tokens(self) -> None:
        score = _keyword_token_hit_score(
            "Game of Thrones",
            "Game.Thrones.S01.2160p.WEB-DL",
        )

        self.assertGreater(score, 0)
        self.assertLess(score, 80)

    def test_partial_multi_word_match_ranks_above_unrelated_title(self) -> None:
        common = {
            "supported": True,
            "provider_priority": 100,
            "duplicate_count": 1,
            "quality_tags": [],
        }
        matching = {**common, "title": "Game.Thrones.S01.Complete"}
        unrelated = {**common, "title": "Unrelated.Series.S01.Complete"}

        self.assertGreater(
            SearchAggregator._score_item(matching, "Game of Thrones"),
            SearchAggregator._score_item(unrelated, "Game of Thrones"),
        )

    def test_duplicate_query_tokens_do_not_inflate_score(self) -> None:
        self.assertEqual(
            _keyword_token_hit_score("show show season", "Show.S01"),
            _keyword_token_hit_score("show season", "Show.S01"),
        )

    def test_clean_title_ranks_above_keyword_buried_in_long_title(self) -> None:
        clean = {"title": "盲盒 2025 1080P"}
        noisy = {"title": "国产直播高颜值合集随机内容大量无关标签盲盒超长版本附加说明和推广信息"}

        self.assertGreater(
            _relevance_score(clean, "盲盒"),
            _relevance_score(noisy, "盲盒"),
        )

    def test_specific_chinese_title_survives_site_prefix_and_release_metadata(self) -> None:
        item = {
            "title": (
                "不太灵免费影视网站 www.butailing.com "
                "超时空同居[60帧率版本][国语配音+中文字幕] "
                "How.Long.Will.I.Love.U.2018.2160p"
            ),
            "source_type": "magnet",
            "url": "magnet:?xt=urn:btih:3333333333333333333333333333333333333333",
            "supported": True,
        }

        self.assertGreaterEqual(_relevance_score(item, "超时空同居"), 92)

    def test_specific_title_magnet_can_outrank_exact_regular_cloud_result(self) -> None:
        magnet = {
            "title": (
                "不太灵免费影视网站 www.butailing.com "
                "超时空同居[60帧率版本][国语配音+中文字幕] "
                "How.Long.Will.I.Love.U.2018.2160p"
            ),
            "source_type": "magnet",
            "url": "magnet:?xt=urn:btih:4444444444444444444444444444444444444444",
            "supported": True,
        }
        cloud = {
            "title": "超时空同居",
            "source_type": "quark",
            "url": "https://pan.quark.cn/s/exact-title",
            "supported": True,
        }

        result = SearchAggregator([_StaticProvider([cloud, magnet])]).search("超时空同居")

        self.assertEqual(magnet["title"], result["items"][0]["title"])

    def test_route_advantage_is_limited_to_ten_or_twenty_relevance_points(self) -> None:
        mobile = {"source_type": "cloud139", "supported": True}
        magnet = {"source_type": "magnet", "supported": True}
        cloud = {"source_type": "quark", "supported": True}

        self.assertGreater(_ranking_score(mobile, 80), _ranking_score(cloud, 100))
        self.assertGreater(_ranking_score(magnet, 90), _ranking_score(cloud, 100))
        self.assertGreater(_ranking_score(mobile, 80), _ranking_score(magnet, 90))
        self.assertGreater(_ranking_score(cloud, 100), _ranking_score(mobile, 79))

    def test_unrelated_magnet_does_not_outrank_exact_cloud_match(self) -> None:
        magnet = {
            "title": "完全无关的资源合集",
            "source_type": "magnet",
            "url": "magnet:?xt=urn:btih:1111111111111111111111111111111111111111",
            "supported": True,
        }
        cloud = {
            "title": "盲盒",
            "source_type": "quark",
            "url": "https://pan.quark.cn/s/exact-match",
            "supported": True,
        }

        result = SearchAggregator([_StaticProvider([magnet, cloud])]).search("盲盒")

        self.assertEqual(["盲盒", "完全无关的资源合集"], [item["title"] for item in result["items"]])

    def test_search_ranking_does_not_filter_adult_query_results(self) -> None:
        item = {
            "title": "成人色情资源 1080P",
            "source_type": "magnet",
            "url": "magnet:?xt=urn:btih:2222222222222222222222222222222222222222",
            "supported": True,
        }

        result = SearchAggregator([_StaticProvider([item])]).search("成人色情")

        self.assertEqual(1, len(result["items"]))
        self.assertEqual(item["title"], result["items"][0]["title"])

    def test_public_projection_preserves_backend_ranking_fields(self) -> None:
        projected = _public_resource_item(
            {
                "title": "测试资源",
                "rank": 2,
                "relevance_score": 86,
                "ranking_score": 106130086,
            },
            public_id="RS-ranking",
        )

        self.assertEqual(2, projected["rank"])
        self.assertEqual(86, projected["relevance_score"])
        self.assertEqual(106130086, projected["ranking_score"])


if __name__ == "__main__":
    unittest.main()
