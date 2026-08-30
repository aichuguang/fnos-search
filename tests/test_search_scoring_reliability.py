from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import unittest

from fnos_media_import.public_web import _public_resource_item
from fnos_media_import.search.aggregator import (
    SearchAggregator,
    _keyword_token_hit_score,
    _ranking_score,
    _relevance_score,
)


class _StaticProvider(RepositoryFakeMixin):
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

    def test_exact_title_outranks_noisy_release_title_regardless_of_source_kind(self) -> None:
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

        self.assertEqual(cloud["title"], result["items"][0]["title"])

    def test_source_kind_never_adds_route_convenience_to_ranking(self) -> None:
        mobile = {"source_type": "cloud139", "supported": True}
        magnet = {"source_type": "magnet", "supported": True}
        cloud = {"source_type": "quark", "supported": True}

        self.assertEqual(_ranking_score(mobile, 90), _ranking_score(magnet, 90))
        self.assertEqual(_ranking_score(magnet, 90), _ranking_score(cloud, 90))
        self.assertGreater(_ranking_score(cloud, 100), _ranking_score(mobile, 99))

    def test_configured_provider_priority_can_bridge_a_small_relevance_gap(self) -> None:
        preferred = {"supported": True, "result_provider_priority_bonus": 3}
        ordinary = {"supported": True, "result_provider_priority_bonus": 0}

        self.assertGreater(
            _ranking_score(preferred, 95),
            _ranking_score(ordinary, 96),
        )

    def test_preference_cannot_rescue_a_low_relevance_result(self) -> None:
        preferred = {"supported": True, "result_provider_priority_bonus": 10}
        ordinary = {"supported": True, "result_provider_priority_bonus": 0}

        # Relevance is bucketed: below the "related" threshold even the top
        # priority cannot lift the result past a clearly related one.
        self.assertLess(
            _ranking_score(preferred, 50),
            _ranking_score(ordinary, 96),
        )

    def test_unrelated_result_stays_at_the_bottom_even_for_top_priority(self) -> None:
        preferred = {"supported": True, "result_provider_priority_bonus": 10}
        ordinary = {"supported": True, "result_provider_priority_bonus": 0}

        self.assertLess(
            _ranking_score(preferred, 20),
            _ranking_score(ordinary, 60),
        )

    def test_preferred_provider_outranks_exact_match_of_lower_priority(self) -> None:
        exact = {"supported": True, "result_provider_priority_bonus": 0}
        preferred = {"supported": True, "result_provider_priority_bonus": 3}

        # Resource titles are noisy; within the same relevance tier the
        # configured source priority decides, even against exact matches.
        self.assertGreater(
            _ranking_score(preferred, 99),
            _ranking_score(exact, 100),
        )

    def test_same_priority_results_keep_relevance_ordering(self) -> None:
        strong = {"supported": True, "result_provider_priority_bonus": 0}
        weak = {"supported": True, "result_provider_priority_bonus": 0}

        self.assertGreater(
            _ranking_score(strong, 100),
            _ranking_score(weak, 92),
        )

    def test_top_priority_provider_bridges_a_moderate_relevance_gap(self) -> None:
        preferred = {"supported": True, "result_provider_priority_bonus": 10}
        ordinary = {"supported": True, "result_provider_priority_bonus": 0}

        self.assertGreater(
            _ranking_score(preferred, 88),
            _ranking_score(ordinary, 96),
        )

    def test_top_priority_provider_wins_same_relevance_band(self) -> None:
        preferred = {"supported": True, "result_provider_priority_bonus": 10}
        ordinary = {"supported": True, "result_provider_priority_bonus": 0}

        # The 99-cap used to swallow the bonus entirely in this band.
        self.assertGreater(
            _ranking_score(preferred, 99),
            _ranking_score(ordinary, 99),
        )
        self.assertGreater(
            _ranking_score(preferred, 92),
            _ranking_score(ordinary, 99),
        )

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


class SearchNoiseToleranceTests(unittest.TestCase):
    def test_unicode_roman_numeral_title_matches_digit_query(self) -> None:
        item = {"title": "流浪地球Ⅱ.The.Wandering.Earth.2023.2160p"}

        self.assertGreaterEqual(_relevance_score(item, "流浪地球2"), 90)

    def test_ascii_roman_numeral_after_cjk_title_matches_digit_query(self) -> None:
        item = {"title": "流浪地球 II 4K国语音轨"}

        self.assertGreaterEqual(_relevance_score(item, "流浪地球2"), 90)

    def test_fullwidth_digit_title_matches_halfwidth_query(self) -> None:
        item = {"title": "沙丘２超清中字"}

        self.assertGreaterEqual(_relevance_score(item, "沙丘2"), 90)

    def test_chinese_season_suffix_matches_digit_title(self) -> None:
        item = {"title": "沙丘2"}

        self.assertEqual(100, _relevance_score(item, "沙丘：第二部"))
        self.assertEqual(100, _relevance_score(item, "沙丘第二部"))

    def test_ad_shell_magnet_ranks_above_collection_junk_for_short_keyword(self) -> None:
        ad_shell = {
            "title": "【高清影视之家发布 www.SSDSSE.com】三体",
            "source_type": "magnet",
            "url": "magnet:?xt=urn:btih:5555555555555555555555555555555555555555",
            "supported": True,
        }
        junk = {
            "title": "三体大合集300部热门影视打包持续更新",
            "source_type": "quark",
            "url": "https://pan.quark.cn/s/collection-junk",
            "supported": True,
        }

        result = SearchAggregator([_StaticProvider([junk, ad_shell])]).search("三体")

        self.assertEqual(ad_shell["title"], result["items"][0]["title"])

    def test_ad_shell_magnet_ranks_above_marketing_junk_release_name(self) -> None:
        ad_shell = {
            "title": "【高清影视之家发布 www.SSDSSE.com】流浪地球2",
            "source_type": "magnet",
            "url": "magnet:?xt=urn:btih:6666666666666666666666666666666666666666",
            "supported": True,
        }
        junk = {
            "title": "流浪地球2全集打包热门影视资源持续更新合集4K HDR 杜比视界",
            "source_type": "quark",
            "url": "https://pan.quark.cn/s/marketing-junk",
            "supported": True,
        }

        result = SearchAggregator([_StaticProvider([junk, ad_shell])]).search("流浪地球2")

        self.assertEqual(ad_shell["title"], result["items"][0]["title"])

    def test_resource_label_prefix_still_scores_high(self) -> None:
        item = {"title": "资源名称：[流浪地球2][意大利版原盘 全景声国语 DIY官中字幕]"}

        # The real title lives inside the first bracket; it must reach the top band.
        self.assertGreaterEqual(_relevance_score(item, "流浪地球2"), 99)

    def test_clean_title_of_verbose_query_gets_reverse_containment_score(self) -> None:
        item = {"title": "三体"}

        self.assertGreaterEqual(_relevance_score(item, "三体 刘慈欣"), 85)

    def test_reverse_containment_does_not_cross_sequel_numbers(self) -> None:
        item = {"title": "沙丘"}

        # "沙丘" is a prefix of the query but the sequel digit must not match.
        self.assertLess(_relevance_score(item, "沙丘2"), 60)

    def test_clean_release_name_outranks_junk_collection_with_same_prefix(self) -> None:
        clean = {"title": "流浪地球2 国语中字"}
        junk = {"title": "流浪地球2全集打包热门影视资源持续更新合集"}

        self.assertGreater(
            _relevance_score(clean, "流浪地球2"),
            _relevance_score(junk, "流浪地球2"),
        )

    def test_wechat_mega_collection_drops_below_related_band(self) -> None:
        item = {
            "title": (
                "32添加微信1730014913自动进机器人群-一切皆有可能（60集）-一球成名（62集）-"
                "一胎四宝拐个爹地给妈咪（70集）伊拉-衣锦荣归（81集）-异人下山（61集）杜培源-隐龙神婿（81集）-"
            ),
        }

        # 关键词子串命中的合集垃圾必须掉出"相关"档位。
        self.assertLess(_relevance_score(item, "荣归"), 60)

    def test_wechat_mega_collection_ranks_below_clean_exact_match(self) -> None:
        junk = {
            "title": (
                "32添加微信1730014913自动进机器人群-一切皆有可能（60集）-一球成名（62集）-"
                "一胎四宝拐个爹地给妈咪（70集）伊拉-衣锦荣归（81集）-异人下山（61集）杜培源-隐龙神婿（81集）-"
            ),
            "source_type": "magnet",
            "url": "magnet:?xt=urn:btih:7777777777777777777777777777777777777777",
            "supported": True,
        }
        cloud = {
            "title": "荣归",
            "source_type": "quark",
            "url": "https://pan.quark.cn/s/clean-title",
            "supported": True,
        }

        result = SearchAggregator([_StaticProvider([junk, cloud])]).search("荣归")

        self.assertEqual(cloud["title"], result["items"][0]["title"])

    def test_wechat_watermark_ad_shell_keeps_high_relevance(self) -> None:
        item = {"title": "【微信防走丢 www.example.com】流浪地球2"}

        # 引流信号在壳上；剥壳后的干净变体必须照常拿高分。
        self.assertGreaterEqual(_relevance_score(item, "流浪地球2"), 90)

    def test_single_episode_count_title_is_not_junk(self) -> None:
        item = {"title": "甄嬛传（76集）全剧集"}

        self.assertGreaterEqual(_relevance_score(item, "甄嬛传"), 60)


if __name__ == "__main__":
    unittest.main()
