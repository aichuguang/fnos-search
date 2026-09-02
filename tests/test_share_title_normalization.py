from __future__ import annotations

import unittest

from fnos_media_import.services.public_submission_preparation_service import (
    PublicSubmissionPreparationService,
    normalize_share_title,
)
from fnos_media_import.web_input import PublicInputError, _limited_text

_SHARE_TEXT = (
    "电影🗄 抓特务(2026) 4K HQ高码率📜介绍：一场警察与特务的较量，却展开满是烟火与温情的百姓人生。"
    "新中国成立之初，派出所所长肖大力（雷佳音 饰）凭直觉锁定潜伏在土唐刀胡同15号院的特务“5182”冯静波（胡歌 饰）。"
    "为追查真相，肖大力索性搬进院落，昔日追查者与目标成了朝夕相处的邻居。"
    "两人的交集，牵动着两个家庭、两代人的命运，岁月起落间满是波折与变故。"
    "当阅尽半生风雨，这对相伴相斗一辈子的冤家，早已不再是单纯的宿敌。"
    "影片根据张策的小说《无悔追踪》改编。"
    "移动：https://yun.139.com/shareweb/#/w/i/2xopeb41sDpeh📁 大小：20.89GB"
    "🏷 标签：#电影 #剧情 #抓特务⬇️【评论区可搜索】 | 🔍网盘专搜"
)


class NormalizeShareTitleTests(unittest.TestCase):
    def test_extracts_core_title_from_full_share_text(self) -> None:
        self.assertEqual(normalize_share_title(_SHARE_TEXT, 300), "抓特务(2026) 4K HQ高码率")

    def test_truncates_instead_of_rejecting_long_titles(self) -> None:
        noisy = "收藏版" * 400 + "📜介绍：正文"
        self.assertEqual(len(normalize_share_title(noisy, 300)), 300)

    def test_plain_title_passes_through_unchanged(self) -> None:
        self.assertEqual(normalize_share_title("  让子弹飞 2010 4K HDR  ", 300), "让子弹飞 2010 4K HDR")

    def test_description_only_text_falls_back_to_clipped_original(self) -> None:
        text = "📜介绍：" + "剧情" * 200
        self.assertEqual(normalize_share_title(text, 300), text[:300].rstrip())

    def test_empty_value_returns_empty_string(self) -> None:
        self.assertEqual(normalize_share_title(None, 300), "")
        self.assertEqual(normalize_share_title("", 300), "")


class PublicSubmissionShareTitleTests(unittest.TestCase):
    @staticmethod
    def _service(cached: dict | None):
        return PublicSubmissionPreparationService(
            search_cache=lambda public_id: cached if public_id == "PUBLIC-1" else None,
            categories=lambda: {"movie": {"label": "电影"}},
            category=lambda _key: {"label": "电影"},
            limited_text=_limited_text,
            validate_url=lambda value, _security: str(value or ""),
            safe_string_list=lambda *_args, **_kwargs: [],
            safe_quark_selection=lambda _value: {},
            safe_cloud139_selection=lambda _value: {},
            detect_link=lambda _url, password="": type("Link", (), {"provider_kind": "mobile"})(),
            security_config=lambda: {},
            config_int=lambda _config, _key, default: default,
        )

    def test_cached_submission_accepts_long_share_text_title(self) -> None:
        cached = {
            "title": _SHARE_TEXT,
            "source_url": "https://yun.139.com/shareweb/#/w/i/2xopeb41sDpeh",
            "password": "",
        }
        prepared = self._service(cached).prepare({"public_id": "PUBLIC-1", "category": "movie"})
        self.assertEqual(prepared.payload["title"], "抓特务(2026) 4K HQ高码率")

    def test_manual_title_over_limit_is_still_rejected(self) -> None:
        with self.assertRaises(PublicInputError):
            self._service(None).prepare(
                {"title": "手工" * 200, "url": "https://example.com/s/1", "category": "movie"}
            )


if __name__ == "__main__":
    unittest.main()
